#!/usr/bin/python
"""
Bulk download script for ASF Data
Author: Y-W. Chen
E-mail: mkcchenyaowen@gmail.com || ORCID: https://orcid.org/0000-0002-1290-001X
Date: Dec. 23, 2024

1. Requirements:
    - Python 3.6 or newer
    - Libraries: requests, hashlib, shutil

2. Usage:
In a terminal/command line, navigate to the directory containing this script, then execute:
    python ./bulk_downloader.py
With embedded URLs:
    - Download the hardcoded list of files defined in the 'urls =' block below.

3. Author's Note:
    This script is my personal solution to address the "Could not get file HEAD" error (https://forum.earthdata.nasa.gov/viewtopic.php?t=6287).
    It is provided "as is" without any warranty. Users are responsible for adhering to the terms of service of ASF DAAC and NASA Earthdata.
    This project is licensed under the MIT License.
    Copyright (c) 2024 Y-W. Chen
    **It is recommended to use the official bulk download script whenever possible.**

4. Reference:
For more details on bulk downloads, refer to:
    https://asf.alaska.edu/how-to/data-tools/data-tools/#bulk_download

Best wishes :)
"""

import os
import sys
import time
import shutil
import hashlib
import requests
import getpass
from http.cookiejar import MozillaCookieJar
from urllib.parse import urlparse

class BulkDownloader:
    def __init__(self, file_urls):
        self.file_urls = file_urls
        self.cookie_jar_path = os.path.join(os.path.expanduser('~'), ".bulk_download_cookiejar.txt")
        self.cookie_jar = MozillaCookieJar(self.cookie_jar_path)
        self.total_bytes = 0
        self.total_time = 0
        self.success = []
        self.failed = []

        if os.path.exists(self.cookie_jar_path):
            self.cookie_jar.load()

        # Ensure the cookie for authentication is valid or retrieve a new one if necessary.
        self.check_or_get_cookie()

    def check_or_get_cookie(self):
        test_url = "https://urs.earthdata.nasa.gov/profile"
        headers = {"User-Agent": "BulkDownloader"}

        try:
            response = requests.get(test_url, cookies=self.cookie_jar, headers=headers, timeout=10)
            if response.status_code == 200:
                print("Cookie validated successfully.")
                return
        except requests.RequestException as e:
            print(f"Cookie validation error: {e}")

        print("Obtaining new cookie...")
        self.get_new_cookie()

    def get_new_cookie(self):
        username = input("Enter Earthdata username: ")
        password = getpass.getpass("Enter Earthdata password: ")
        auth_url = "https://urs.earthdata.nasa.gov/oauth/authorize"
        
        session = requests.Session()
        session.auth = (username, password)
        response = session.get(auth_url, cookies=self.cookie_jar)

        if response.status_code == 200:
            self.cookie_jar.save(ignore_discard=True, ignore_expires=True)
            print("Cookie obtained and saved.")
        else:
            print(f"Failed to obtain cookie. Status code: {response.status_code}")
            sys.exit(1)

    def calculate_md5(self, file_path):
        hash_md5 = hashlib.md5()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_md5.update(chunk)
        return hash_md5.hexdigest()

    def verify_file_integrity(self, local_filename, expected_md5):
        if expected_md5:
            actual_md5 = self.calculate_md5(local_filename)
            if actual_md5 != expected_md5:
                raise ValueError(f"MD5 mismatch for {local_filename}. Expected {expected_md5}, got {actual_md5}.")

    def download_file(self, url, expected_md5=None):
        local_filename = os.path.basename(urlparse(url).path)
        headers = {"User-Agent": "BulkDownloader"}

        try:
            if os.path.exists(local_filename):
                local_size = os.path.getsize(local_filename)
                total_size = int(requests.head(url, headers=headers, cookies=self.cookie_jar).headers.get('Content-Length', 0))

                if local_size == total_size:
                    print(f"File already downloaded completely: {local_filename}")
                    self.verify_file_integrity(local_filename, expected_md5)
                    self.success.append(local_filename)
                    self.total_bytes += local_size
                    return
                else:
                    headers["Range"] = f"bytes={local_size}-"
                    print(f"File not downloaded completely: {local_filename}, restarting download...")
            else:
                local_size = 0
                print(f"Downloading {url}...")

            with requests.get(url, cookies=self.cookie_jar, headers=headers, stream=True, timeout=30) as response:
                response.raise_for_status()
                total_size = int(response.headers.get('Content-Length', 0)) + local_size
                downloaded_size = local_size

                mode = 'ab' if local_size > 0 else 'wb'
                with open(local_filename, mode) as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                            downloaded_size += len(chunk)
                            self.show_progress(downloaded_size, total_size)

                if downloaded_size != total_size:
                    print(f"Warning: not downloaded completely: {local_filename}")
                    raise ValueError(f"Downloaded file size ({downloaded_size}) does not match expected size ({total_size}).")

            file_size = os.path.getsize(local_filename)
            if file_size != total_size:
                raise ValueError(f"Final file size ({file_size}) does not match expected size ({total_size}).")

            self.verify_file_integrity(local_filename, expected_md5)
            self.total_bytes += file_size
            self.success.append(local_filename)
            print(f"\nDownloaded {local_filename} ({file_size} bytes).")

        except (requests.RequestException, ValueError) as e:
            self.failed.append(url)
            print(f"Failed to download {url}: {e}")

    def show_progress(self, downloaded, total):
        percent = (downloaded / total) * 100 if total else 0
        sys.stdout.write(f"\rProgress: {downloaded}/{total} bytes ({percent:.2f}%)")
        sys.stdout.flush()

    def download_files(self):
        start_time = time.time()

        for url in self.file_urls:
            # Example: Add MD5 values here if available for each file.
            expected_md5 = None  # Replace with actual MD5 value if known.
            self.download_file(url, expected_md5=expected_md5)

        self.total_time = time.time() - start_time

    def print_summary(self):
        print("\nDownload Summary")
        print("-----------------------------")
        print(f"Total bytes downloaded: {self.total_bytes}")
        print(f"Time taken: {self.total_time:.2f} seconds")

        if self.success:
            print("\nSuccessfully downloaded files:")
            for f in self.success:
                print(f" - {f}")

        if self.failed:
            print("\nFailed downloads:")
            for f in self.failed:
                print(f" - {f}")

if __name__ == "__main__":
    # Replace with actual URLs or load from a file
    urls = [
        "https://datapool.asf.alaska.edu/SLC/SA/S1A_IW_SLC__xxxx1.zip",
        "https://datapool.asf.alaska.edu/SLC/SA/S1A_IW_SLC__xxxx2.zip"
        ]

    downloader = BulkDownloader(urls)
    downloader.download_files()
    downloader.print_summary()
