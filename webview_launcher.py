import argparse
import webview

def main():
    parser = argparse.ArgumentParser(description='Launch a pywebview window with custom attributes.')
    parser.add_argument('--url', type=str, required=True, help='The URL to load in the webview')
    parser.add_argument('--title', type=str, default='Conductor', help='Window title')
    parser.add_argument('--width', type=int, default=800, help='Window width')
    parser.add_argument('--height', type=int, default=600, help='Window height')
    parser.add_argument('--resizable', action='store_true', help='Make the window resizable')
    parser.add_argument('--frameless', action='store_true', help='Create a frameless window')
    parser.add_argument('--fullscreen', action='store_true', help='Launch in fullscreen mode')

    args = parser.parse_args()

    webview.create_window(
        title=args.title,
        url=args.url,
        width=args.width,
        height=args.height,
        resizable=args.resizable,
        frameless=args.frameless,
        fullscreen=args.fullscreen
    )
    webview.start()

if __name__ == '__main__':
    main()
