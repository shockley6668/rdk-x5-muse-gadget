#!/usr/bin/env python3
"""Tell Muse where to find the pet controls in its existing Linux tool list."""


def advertise_pet_controls(commands):
    commands["system.run"]["description"] += (
        " On this RDK X5, petctl controls the desktop pet. Run petctl --help"
        " for commands and petctl status to check the result."
    )


if __name__ == "__main__":
    from musegadget import cli
    from musegadget.executor import COMMAND_SPECS

    advertise_pet_controls(COMMAND_SPECS)
    raise SystemExit(cli.main(["run"]))
