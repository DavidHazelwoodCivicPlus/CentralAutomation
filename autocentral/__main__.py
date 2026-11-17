import sys

if len(sys.argv) > 1 and sys.argv[1] != "--gui":
    from .cli import main
    sys.exit(main(sys.argv[1:]))

from .gui import main as gui_main

gui_main()
