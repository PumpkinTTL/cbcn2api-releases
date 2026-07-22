#!/usr/bin/env python3
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import webview
from src.gui.app import GuiApi
from src.gui.html import HTML


def main():
    api = GuiApi()

    window = webview.create_window(
        "CB/WorkBuddy Manager",
        html=HTML,
        js_api=api,
        width=1200,
        height=820,
        min_size=(900, 600),
        resizable=True,
    )

    webview.start(
        private_mode=False,
        debug=False,
    )


if __name__ == "__main__":
    main()
