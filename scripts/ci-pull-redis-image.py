#!/usr/bin/env python3
"""Print only the verified immutable CI Redis reference; never use AWS credentials."""
import argparse
from ci_image_transport import pull

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    args = parser.parse_args()
    print(pull(args.image))
