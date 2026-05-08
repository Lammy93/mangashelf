from collections import OrderedDict
import threading
import time

class LRUCache:
    def __init__(self, maxsize=128, ttl=60):
        self.maxsize = maxsize
        self.ttl = ttl
        self.data = OrderedDict()
        self.timestamps = {}
        self.lock = threading.Lock()

    def get(self, key):
        with self.lock:
            if key in self.data:
                if time.time() - self.timestamps[key] < self.ttl:
                    self.data.move_to_end(key)
                    return self.data[key]
                del self.data[key]
                del self.timestamps[key]
            return None

    def put(self, key, value):
        with self.lock:
            if key in self.data:
                self.data.move_to_end(key)
            elif len(self.data) >= self.maxsize:
                oldest = next(iter(self.data))
                del self.data[oldest]
                del self.timestamps[oldest]
            self.data[key] = value
            self.timestamps[key] = time.time()

    def invalidate(self, key):
        with self.lock:
            self.data.pop(key, None)
            self.timestamps.pop(key, None)

_metadata_cache = LRUCache(maxsize=256, ttl=120)
