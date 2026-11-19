"""Vérifie le contrôle des fichiers --answers. Lancer : python tests/test_answers.py"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from felcloud.wizard import validate_answers  # noqa: E402

GOOD = {
    "version": 1, "project": "demo-web", "tool": "terraform", "cloud": "felcloud",
    "network": {"mode": "create", "cidr": "10.0.5.0/24", "dns": ["1.1.1.1"], "external_network": "INTERNET"},
    "security_group": {"rules": ["ssh"], "ssh_cidr": "0.0.0.0/0", "custom_tcp_ports": [8080]},
    "keypair": {"mode": "generate", "name": "demo-web-key", "public_key_path": "~/.ssh/demo-web-key.pub",
                "private_key_path": "~/.ssh/demo-web-key"},
    "instances": [{"name": "web-1", "image": "Ubuntu 24.04", "flavor": "a1.small", "floating_ip": True,
                   "nginx": True, "ssh_user": "ubuntu", "role": ""}],
    "internet_proxy": None,
    "volumes": [{"name": "data-1", "size": 10, "type": "", "attach_to": "web-1"}],
}


def bad(**changes):
    return validate_answers({**GOOD, **changes})


def demo():
    assert validate_answers(GOOD) == [], validate_answers(GOOD)
    assert validate_answers("texte")
    assert bad(project="../../etc")                       # sortie du dossier des projets
    assert bad(project='x"\nresource "evil" "y" {}')      # injection HCL
    assert bad(tool="bash")
    assert bad(cloud='a"""\nimport os')                   # injection dans le docstring Python
    assert bad(keypair={**GOOD["keypair"], "private_key_path": "~/k' ; rm -rf ~ ; '"})
    assert bad(network={**GOOD["network"], "cidr": "8.8.8.0/24"})
    assert bad(network={**GOOD["network"], "external_network": "x\n}"})
    assert bad(security_group={**GOOD["security_group"], "custom_tcp_ports": [70000]})
    assert bad(internet_proxy={"bastion": "absent"})
    assert bad(volumes=[{**GOOD["volumes"][0], "attach_to": "absent"}])
    print("ok")


if __name__ == "__main__":
    demo()
