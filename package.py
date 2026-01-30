import os

name = "kitsu"
title = "Kitsu"
version = "1.2.6-dev.7"
client_dir = "ayon_kitsu"
studio_name = os.getenv("AYON_STUDIO_NAME")

services = {
    "processor": {"image": f"{studio_name}/ayon-kitsu-processor:{version}"},
}

ayon_required_addons = {
    "core": ">=0.3.0",
}
ayon_compatible_addons = {}
