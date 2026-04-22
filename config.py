"""Config manager."""
import json, os


class Config:
    def __init__(self, path):
        self.path = path
        self.name = "Kid"
        self.channel = 1
        self.silent = False

    def load(self):
        try:
            with open(self.path) as f:
                data = json.load(f)
                self.name = data.get("name", self.name)
                self.channel = data.get("channel", self.channel)
                self.silent = data.get("silent", self.silent)
        except (FileNotFoundError, json.JSONDecodeError):
            self.save()

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        # Atomic write: tmp + fsync + rename. Prevents a power cut
        # mid-save from corrupting config.json and losing the user's
        # name / channel / silent preference. Matters especially for
        # silent-mode toggle which writes config every time it flips.
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(
                {"name": self.name, "channel": self.channel, "silent": self.silent},
                f, indent=2,
            )
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp, self.path)
