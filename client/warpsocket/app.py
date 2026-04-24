import logging

from warpsocket import __version__


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logging.info("WarpSocket client v%s — scaffolding stub", __version__)
    return 0
