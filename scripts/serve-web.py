from __future__ import annotations

import argparse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class SpaStaticHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, directory: str | None = None, **kwargs):
        super().__init__(*args, directory=directory, **kwargs)

    def do_GET(self) -> None:  # noqa: N802
        requested_path = Path(self.translate_path(self.path))

        if requested_path.exists() and requested_path.is_file():
            return super().do_GET()

        self.path = "/index.html"
        return super().do_GET()


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the built frontend with SPA fallback.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5173)
    parser.add_argument("--dir", required=True)
    args = parser.parse_args()

    directory = str(Path(args.dir).resolve())

    def handler(*handler_args, **handler_kwargs):
        return SpaStaticHandler(*handler_args, directory=directory, **handler_kwargs)

    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"Serving frontend on http://{args.host}:{args.port} from {directory}")
    server.serve_forever()


if __name__ == "__main__":
    main()
