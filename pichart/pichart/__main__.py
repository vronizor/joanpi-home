import logging
import os
import threading

from . import config
from .collector import Collector
from .store import Store
from .web import serve


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = config.load()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    store = Store(cfg.data_dir / "pichart.sqlite")
    collector = Collector(cfg, store)
    threading.Thread(target=collector.run, name="collector", daemon=True).start()
    host = os.environ.get("PICHART_HOST", "127.0.0.1")
    logging.info("Pi Chart on %s:%s", host, cfg.port)
    serve(host, cfg.port, collector, store)


if __name__ == "__main__":
    main()
