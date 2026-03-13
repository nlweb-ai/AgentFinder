#!/usr/bin/env python3
"""
Download M365 Apps data from ES Explorer.

This script helps automate downloading M365 apps data.
You'll need to provide authentication cookies from your browser session.
"""
import os
import json
import argparse
from pathlib import Path


def get_browser_cookies():
    """
    Instructions for getting authentication cookies from your browser.

    Steps:
    1. Open https://aka.ms/esexplorer in your browser
    2. Log in with your Microsoft account
    3. Open Developer Tools (F12)
    4. Go to Network tab
    5. Make a request (click around in ES Explorer)
    6. Find a request to visualstudio.com
    7. Right-click > Copy > Copy as cURL
    8. Extract the Cookie header
    """
    print("""
    TO GET YOUR AUTHENTICATION COOKIES:

    1. Open https://aka.ms/esexplorer in Chrome/Edge
    2. Log in with your Microsoft account
    3. Press F12 to open Developer Tools
    4. Go to Application tab > Cookies
    5. Find cookies for visualstudio.com domain
    6. Copy the cookie values and save to a file named 'cookies.json':

    {
        "VstsSession": "your-session-cookie",
        ".AspNetCore.Cookies": "your-aspnet-cookie",
        "VstsRememberMe": "your-remember-me-cookie"
    }

    Save this file in the same directory as this script.
    """)


def download_with_cookies(cookie_file: str, output_dir: str):
    """
    Download M365 apps data using provided cookies.

    Args:
        cookie_file: Path to JSON file with cookies
        output_dir: Directory to save downloaded data
    """
    import requests

    if not os.path.exists(cookie_file):
        print(f"Error: Cookie file not found: {cookie_file}")
        print("\nRun with --help-cookies to see instructions.")
        return False

    # Load cookies
    with open(cookie_file) as f:
        cookies = json.load(f)

    # ES Explorer API endpoints (you'll need to find the actual endpoint)
    # These are placeholder URLs - you'll need to update them
    urls = {
        "schema": "https://o365exchange.visualstudio.com/DefaultCollection/O365%20Core/_git/EntityServe?path=/sources/dev/Schema/EntityDefinitions/EntitySchema/AppsEntitySchema.settings.ini",
        "apps_types": "https://o365exchange.visualstudio.com/O365%20Core/_wiki/wikis/O365%20Core.wiki/549848/Apps-Types",
    }

    session = requests.Session()
    session.cookies.update(cookies)

    # Download schema
    print("Downloading schema...")
    try:
        response = session.get(urls["schema"])
        if response.status_code == 200:
            schema_path = Path(output_dir) / "AppsEntitySchema.settings.ini"
            schema_path.write_text(response.text)
            print(f"✓ Schema saved to {schema_path}")
        else:
            print(f"✗ Schema download failed: {response.status_code}")
            print(f"  Response: {response.text[:200]}")
    except Exception as e:
        print(f"✗ Schema download error: {e}")

    print("\nNOTE: For the full Apps Index, you'll need to:")
    print("1. Go to https://aka.ms/esexplorer")
    print("2. User Data > Download Indexes > Apps Index")
    print("3. Click Download and save to this directory")
    print("4. Run the CIDebug tool to convert the binary format")

    return True


def main():
    parser = argparse.ArgumentParser(
        description="Download M365 Apps data from ES Explorer"
    )
    parser.add_argument(
        "--cookies",
        default="cookies.json",
        help="Path to JSON file with authentication cookies"
    )
    parser.add_argument(
        "--output",
        default="data/m365",
        help="Output directory for downloaded data"
    )
    parser.add_argument(
        "--help-cookies",
        action="store_true",
        help="Show instructions for getting cookies"
    )

    args = parser.parse_args()

    if args.help_cookies:
        get_browser_cookies()
        return

    # Create output directory
    os.makedirs(args.output, exist_ok=True)

    # Download data
    download_with_cookies(args.cookies, args.output)


if __name__ == "__main__":
    main()
