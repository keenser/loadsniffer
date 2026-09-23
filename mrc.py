#!/usr/bin/env python3
#
# vim: tabstop=4 expandtab shiftwidth=4 softtabstop=4
#
"""Entry point kept at the repo root so `python3 mrc.py <path>` (Dockerfile's
ENTRYPOINT) keeps working unchanged - the real implementation lives in mrc/."""

from mrc.app import main

if __name__ == '__main__':
    main()
