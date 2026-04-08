
name = "kitsu"
title = "Kitsu"
version = "1.2.6-prod.0.2.20"
client_dir = "ayon_kitsu"

services = {
    "processor": {
        "image": f"ghcr.io/fuzzkingcool/ayon-kitsu-processor:{version}",
        "environment": {
            "AYON_ADDON_NAME": name,
            "AYON_ADDON_VERSION": version,
        },
    },
}

ayon_required_addons = {
    "core": ">=0.3.0",
}
ayon_compatible_addons = {}
