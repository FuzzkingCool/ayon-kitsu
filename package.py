
name = "kitsu"
title = "Kitsu"
version = "1.2.6-dev.16"
client_dir = "ayon_kitsu"

services = {
    "processor": {"image": f"studioname/ayon-kitsu-processor:{version}"},
}

ayon_required_addons = {
    "core": ">=0.3.0",
}
ayon_compatible_addons = {}
