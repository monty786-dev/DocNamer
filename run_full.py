import multiprocessing
from docnamer.gui_full.app import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
