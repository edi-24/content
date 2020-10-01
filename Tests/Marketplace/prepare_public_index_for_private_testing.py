import time

from Tests.Marketplace.upload_packs_private import download_and_extract_index, update_index_with_priced_packs, \
    extract_packs_artifacts
from Tests.Marketplace.marketplace_services import init_storage_client, GCPConfig
import os
import sys
import shutil
import json
import argparse
from datetime import datetime
from demisto_sdk.commands.common.tools import print_error, print_color, LOG_COLORS

MAX_SECONDS_TO_WAIT_FOR_LOCK = 600


def change_pack_price_to_zero(path_to_pack_metadata):
    with open(path_to_pack_metadata, 'r') as pack_metadata_file:
        pack_metadata = json.load(pack_metadata_file)

    pack_metadata['price'] = 0
    with open(path_to_pack_metadata, 'w') as pack_metadata_file:
        json.dump(pack_metadata, pack_metadata_file, indent=4)


def change_packs_price_to_zero(public_index_folder_path):
    paths_to_packs_in_merged_index = [pack_dir.path for pack_dir in os.scandir(public_index_folder_path) if
                                      pack_dir.is_dir()]
    for path_to_pack in paths_to_packs_in_merged_index:
        path_to_pack_metadata = os.path.join(path_to_pack, 'metadata.json')
        change_pack_price_to_zero(path_to_pack_metadata)


def merge_private_index_into_public_index(public_index_folder_path, private_index_folder_path):
    packs_in_private_index = [pack_dir.name for pack_dir in os.scandir(private_index_folder_path) if pack_dir.is_dir()]
    for pack_name in packs_in_private_index:
        path_to_pack_in_private_index = os.path.join(private_index_folder_path, pack_name)
        path_to_pack_in_public_index = os.path.join(public_index_folder_path, pack_name)
        shutil.copy(path_to_pack_in_private_index, path_to_pack_in_public_index)


def upload_modified_index(public_index_folder_path, extract_destination_path, public_ci_dummy_index_blob, build_number,
                          private_packs):
    """Upload updated index zip to cloud storage.

    Args:
        index_folder_path (str): index folder full path.
        extract_destination_path (str): extract folder full path.
        public_ci_dummy_index_blob (Blob): google cloud storage object that represents the dummy index.zip blob.
        build_number (str): circleCI build number, used as an index revision.
        private_packs (list): List of private packs and their price.
        current_commit_hash (str): last commit hash of head.
        index_generation (str): downloaded index generation.

    """
    with open(os.path.join(public_index_folder_path, "index.json"), "w+") as index_file:
        for private_pack in private_packs:
            private_pack['price'] = 0
        index = {
            'revision': build_number,
            'modified': datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
            'packs': private_packs
        }
        json.dump(index, index_file, indent=4)

    index_zip_name = os.path.basename(public_index_folder_path)
    index_zip_path = shutil.make_archive(base_name=public_index_folder_path, format="zip",
                                         root_dir=extract_destination_path, base_dir=index_zip_name)
    try:
        public_ci_dummy_index_blob.reload()
        public_ci_dummy_index_blob.cache_control = "no-cache,max-age=0"  # disabling caching for index blob
        public_ci_dummy_index_blob.upload_from_filename(index_zip_path)

        print_color("Finished uploading index.zip to storage.", LOG_COLORS.GREEN)
    except Exception as e:
        print_error(f"Failed in uploading index. "
                    f"Mismatch in index file generation, additional info: {e}\n")
        sys.exit(1)
    finally:
        shutil.rmtree(public_index_folder_path)


def option_handler():
    """Validates and parses script arguments.

    Returns:
        Namespace: Parsed arguments object.

    """
    parser = argparse.ArgumentParser(description="Store packs in cloud storage.")
    # disable-secrets-detection-start
    parser.add_argument('-b', '--public_bucket_name', help="CI public bucket name", required=True)
    parser.add_argument('-pb', '--private_bucket_name', help="CI private bucket name", required=True)
    parser.add_argument('-s', '--service_account',
                        help=("Path to gcloud service account, is for circleCI usage. "
                              "For local development use your personal account and "
                              "authenticate using Google Cloud SDK by running: "
                              "`gcloud auth application-default login` and leave this parameter blank. "
                              "For more information go to: "
                              "https://googleapis.dev/python/google-api-core/latest/auth.html"),
                        required=False)
    parser.add_argument('-n', '--ci_build_number',
                        help="CircleCi build number (will be used as hash revision at index file)", required=True)
    parser.add_argument('-e', '--extract_public_index_path', help="Full path of folder to extract the public index",
                        required=True)
    parser.add_argument('-sb', '--storage_base_path', help="Storage base path of the directory to upload to.",
                        required=False),
    parser.add_argument('-p', '--pack_name', help="Modified pack to upload to gcs.")
    parser.add_argument('-a', '--artifacts_path', help="The full path of packs artifacts", required=True)
    parser.add_argument('-ea', '--extract_artifacts_path', help="Full path of folder to extract wanted packs",
                        required=True)
    parser.add_argument('-di', '--dummy_index_dir_path', help="Full path to the dummy index in the private CI bucket",
                        required=True)

    # disable-secrets-detection-end
    return parser.parse_args()


def is_dummy_index_locked(public_storage_bucket, dummy_index_lock_path):
    dummy_index_lock_blob = public_storage_bucket.blob(dummy_index_lock_path)
    return dummy_index_lock_blob.exists()


def lock_dummy_index(public_storage_bucket, dummy_index_lock_path):
    dummy_index_lock_blob = public_storage_bucket.blob(dummy_index_lock_path)
    with open('lock.txt', 'w') as lock_file:
        lock_file.write('locked')

    with open('lock.txt', 'rb') as lock_file:
        dummy_index_lock_blob.upload_from_file(lock_file)


def acquire_dummy_index_lock(public_storage_bucket, dummy_index_lock_path):
    print_error(f"Verifying if dummy index is locked before "
                f"acquiring: {is_dummy_index_locked(public_storage_bucket, dummy_index_lock_path)}")
    total_seconds_waited = 0
    while is_dummy_index_locked(public_storage_bucket, dummy_index_lock_path):
        print_error("dummy index is curently locked")
        if total_seconds_waited >= MAX_SECONDS_TO_WAIT_FOR_LOCK:
            print_error("Error: was not able to acquire dummy index lock.")
            exit(1)
        total_seconds_waited += 10
        time.sleep(10)

    lock_dummy_index(public_storage_bucket, dummy_index_lock_path)
    print_error("Waiting for 5 minutes so I can see the file in the bucket")
    time.sleep(300)

    print_error(f"Verifying if dummy index is locked after "
                f"acquiring: {is_dummy_index_locked(public_storage_bucket, dummy_index_lock_path)}")


def release_dummy_index_lock(public_storage_bucket, dummy_index_lock_path):
    print_error(f"Verifying if dummy index is locked before "
                f"deleting: {is_dummy_index_locked(public_storage_bucket, dummy_index_lock_path)}")

    dummy_index_lock_blob = public_storage_bucket.blob(dummy_index_lock_path)
    dummy_index_lock_blob.delete()

    print_error(f"Verifying if dummy index is locked after "
                f"deleting: {is_dummy_index_locked(public_storage_bucket, dummy_index_lock_path)}")


def main():

    upload_config = option_handler()
    service_account = upload_config.service_account
    build_number = upload_config.ci_build_number
    public_bucket_name = upload_config.public_bucket_name
    private_bucket_name = upload_config.private_bucket_name
    storage_base_path = upload_config.storage_base_path
    extract_public_index_path = upload_config.extract_public_index_path
    changed_pack = upload_config.pack_name
    extract_destination_path = upload_config.extract_artifacts_path
    packs_artifacts_path = upload_config.artifacts_path
    dummy_index_dir_path = upload_config.dummy_index_dir_path
    dummy_index_path = os.path.join(dummy_index_dir_path, 'index.zip')
    dummy_index_lock_path = os.path.join(dummy_index_dir_path, 'lock.txt')

    data_msg = f'dummy_index_dir_path:{dummy_index_dir_path}'
    print_error(data_msg)

    storage_client = init_storage_client(service_account)
    public_storage_bucket = storage_client.bucket(public_bucket_name)
    private_storage_bucket = storage_client.bucket(private_bucket_name)

    dummy_index_dir_blob = public_storage_bucket.blob(dummy_index_dir_path)
    dummy_index_blob = public_storage_bucket.blob(dummy_index_path)

    acquire_dummy_index_lock(public_storage_bucket, dummy_index_lock_path)

    try:
        if storage_base_path:
            GCPConfig.STORAGE_BASE_PATH = storage_base_path

        extract_packs_artifacts(packs_artifacts_path, extract_destination_path)
        public_index_folder_path, public_index_blob, _ = download_and_extract_index(public_storage_bucket,
                                                                                    extract_public_index_path)

        # In order for the packs to be downloaded successfully, their price has to be 0
        change_packs_price_to_zero(public_index_folder_path)

        private_packs, private_index_path, private_index_blob = update_index_with_priced_packs(private_storage_bucket,
                                                                                               extract_destination_path,
                                                                                               public_index_folder_path,
                                                                                               changed_pack, True)
        print_error(f'private packs are: {private_packs}')
        upload_modified_index(public_index_folder_path, extract_public_index_path, dummy_index_blob, build_number,
                              private_packs)
    finally:
        release_dummy_index_lock(public_storage_bucket, dummy_index_lock_path)


if __name__ == '__main__':
    main()
