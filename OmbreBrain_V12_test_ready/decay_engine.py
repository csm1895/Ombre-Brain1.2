class DecayEngine:
    def __init__(self, *args, **kwargs):
        self.is_running = False

    async def ensure_started(self):
        self.is_running = True
        return None

    def start(self):
        self.is_running = True
        return None

    def stop(self):
        self.is_running = False
        return None

    def run_once(self):
        return None

    def calculate_score(self, metadata):
        try:
            return float(metadata.get("importance", 5))
        except Exception:
            return 5.0
