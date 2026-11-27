import multiprocessing
from docnamer.gui_lite.app import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
