"""Config manager."""
import json, os
class Config:
    def __init__(self, path):
        self.path=path; self.name="Kid"; self.channel=1
    def load(self):
        try:
            with open(self.path) as f:
                data=json.load(f); self.name=data.get("name",self.name)
                self.channel=data.get("channel",self.channel)
        except (FileNotFoundError, json.JSONDecodeError): self.save()
    def save(self):
        os.makedirs(os.path.dirname(self.path),exist_ok=True)
        with open(self.path,"w") as f:
            json.dump({"name":self.name,"channel":self.channel},f,indent=2)
