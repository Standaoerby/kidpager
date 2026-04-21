"""Config manager."""
import json, os


class Config:
    def __init__(self, path):
        self.path = path
        self.name = "Kid"
        self.channel = 1
        # Silent mode: when True, all buzzer tones are suppressed. Toggled from
        # the profile menu, persisted here so it survives reboot.
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
        with open(self.path, "w") as f:
            json.dump(
                {"name": self.name, "channel": self.channel, "silent": self.silent},
                f, indent=2,
            )
