"""`felcloud new` : les questions posées à l'utilisateur.

Les réponses forment un dictionnaire indépendant de l'outil (Terraform ou Pulumi).
Il est enregistré dans `felcloud.yaml` du projet généré, puis transformé en code
par les templates de l'outil choisi.

L'assistant est une suite d'étapes, une par question. Échap ramène à la question
précédente, qui propose la réponse déjà donnée : il suffit d'appuyer sur Entrée
pour la garder. Le récapitulatif final permet aussi de modifier une partie.
"""

import ipaddress
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from rich.table import Table

from felcloud import cloud, config, credentials, ui

TOOLS = {
    "terraform": "Terraform",
    "pulumi-python": "Pulumi (Python)",
    "pulumi-javascript": "Pulumi (JavaScript)",
    "pulumi-go": "Pulumi (Go)",
}

TOOL_DESCRIPTIONS = {
    "terraform": "langage HCL, déclaratif : l'outil d'infrastructure le plus répandu",
    "pulumi-python": "l'infrastructure décrite en Python",
    "pulumi-javascript": "l'infrastructure décrite en JavaScript (Node.js)",
    "pulumi-go": "l'infrastructure décrite en Go",
}

# Programmes nécessaires pour chaque outil : (commande, nom affiché, commande d'installation).
REQUIREMENTS = {
    "terraform": [("terraform", "Terraform", "felcloud install terraform")],
    "pulumi-python": [("pulumi", "Pulumi", "felcloud install pulumi")],
    "pulumi-javascript": [("pulumi", "Pulumi", "felcloud install pulumi"),
                          ("node", "Node.js", "felcloud install node"),
                          ("npm", "npm", "felcloud install node")],
    "pulumi-go": [("pulumi", "Pulumi", "felcloud install pulumi"), ("go", "Go", "felcloud install go")],
}


def missing_requirements(tool):
    from felcloud import doctor

    return [(label, how) for cmd, label, how in REQUIREMENTS[tool] if not doctor.find_tool(cmd)]


# (clé, nom, description, coché par défaut)
BRICKS = [
    ("network", "Réseau", "réseau privé + sous-réseau + routeur relié à Internet", True),
    ("security_group", "Pare-feu", "choisir les ports ouverts : SSH, HTTP...", True),
    ("keypair", "Clé SSH", "pour te connecter aux machines sans mot de passe", True),
    ("instances", "Machines virtuelles", "système, taille, nombre", True),
    ("floating_ip", "IP publique", "rendre l'infrastructure joignable depuis Internet", True),
    ("volumes", "Disques supplémentaires", "volumes de stockage attachés aux machines", False),
]

# (clé, nom, description, coché par défaut)
SG_RULES = [
    ("ssh", "SSH (port 22)", "indispensable pour te connecter aux machines", True),
    ("http", "HTTP (port 80)", "site web", False),
    ("https", "HTTPS (port 443)", "site web sécurisé", False),
    ("icmp", "Ping (ICMP)", "tester si une machine répond", True),
    ("internal", "Trafic entre les machines du projet",
     "les machines du projet peuvent se parler librement entre elles", True),
]

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}[a-z0-9]$")
NAME_RULE = "3 à 32 caractères : minuscules, chiffres et tirets, en commençant par une lettre."


def _valid_project(value):
    if not NAME_RE.match(value.strip()):
        return NAME_RULE
    if (config.projects_dir() / value.strip()).exists():
        return f"Le projet {config.projects_dir() / value.strip()} existe déjà : choisis un autre nom."
    return True


def _valid_vm_name(value):
    return True if NAME_RE.match(value.strip()) else NAME_RULE


def _valid_cidr(value):
    try:
        net = ipaddress.ip_network(value.strip(), strict=True)
    except ValueError:
        return "Format attendu : 10.0.5.0/24"
    if net.version != 4 or not net.is_private:
        return "Utilise une plage privée IPv4 (10.x.x.x, 172.16-31.x.x ou 192.168.x.x)."
    if net.prefixlen > 28:
        return "Plage trop petite : /28 au minimum (par exemple /24)."
    return True


def _valid_source(value):
    try:
        ipaddress.ip_network(value.strip(), strict=False)
        return True
    except ValueError:
        return "Format attendu : une adresse comme 197.5.10.20/32, ou une plage comme 197.5.10.0/24"


def _valid_ports(value):
    if not value.strip():
        return True
    for part in value.replace(" ", "").split(","):
        if not part.isdigit() or not 1 <= int(part) <= 65535:
            return f"« {part} » n'est pas un port valide (1 à 65535). Exemple : 8080,3000"
    return True


def _valid_int(low, high):
    def check(value):
        if not value.strip().isdigit() or not low <= int(value) <= high:
            return f"Entre un nombre entre {low} et {high}."
        return True
    return check


def _remaining(limits, key):
    used, maximum = limits.get(key, (0, -1))
    return None if maximum in (-1, None) else maximum - used


def _public_keys():
    """Clés publiques locales dont la clé privée correspondante existe et correspond vraiment."""
    valid = []
    for pub in sorted((Path.home() / ".ssh").glob("*.pub")):
        private = pub.with_suffix("")
        if not private.exists():
            continue
        try:
            derived = subprocess.run(["ssh-keygen", "-y", "-f", str(private)], capture_output=True,
                                     text=True, timeout=5, stdin=subprocess.DEVNULL).stdout.split()[:2]
        except (OSError, subprocess.TimeoutExpired):
            continue
        if derived and derived == pub.read_text().split()[:2]:
            valid.append(pub)
    return valid


def _tilde(path):
    path = Path(path)
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


def _recommended_flavor(flavors):
    """Taille conseillée pour débuter : la plus petite avec 2 vCPU et 2 Go de RAM."""
    fits = [f for f in flavors if f.vcpus >= 2 and f.ram_mb >= 2048 and "optimized" not in f.name]
    return min(fits or flavors, key=lambda f: (f.vcpus, f.ram_mb, f.disk_gb, "basic" not in f.name))


@dataclass
class Step:
    section: str
    ask: Callable                       # pose la ou les questions de l'étape
    when: Callable = lambda: True       # l'étape s'applique-t-elle ?
    silent: bool = False                # ne pose pas de question (calcul, avertissement)


class Wizard:
    """Pose les questions dans l'ordre, avec retour en arrière (Échap).

    `self.st` garde les réponses brutes : quand on revient sur une question, la
    réponse précédente est proposée par défaut."""

    def __init__(self):
        self.st = {}
        self.found = None
        self.found_cloud = None
        self.clouds = list(credentials.installed_clouds())
        if not self.clouds:
            raise ui.Abort("Aucun cloud configuré : lance d'abord « felcloud setup ».")
        s = Step
        self.steps = [
            s("Projet", self.ask_name),
            s("Projet", self.ask_tool),
            s("Projet", self.ask_cloud, when=lambda: len(self.clouds) > 1),
            s("Projet", self.discover, silent=True),
            s("Projet", self.ask_bricks),

            s("Réseau", self.ask_net_mode, when=lambda: self.uses("network") and self.found.project_networks),
            s("Réseau", self.ask_net_existing, when=lambda: self.uses("network") and self.net_mode == "existing"),
            s("Réseau", self.ask_cidr, when=lambda: self.uses("network") and self.net_mode == "create"),
            s("Réseau", self.ask_external, when=lambda: self.uses("network") and self.needs_external
              and len(self.found.external_networks) > 1),

            s("Pare-feu", self.ask_sg_rules, when=lambda: self.uses("security_group")),
            s("Pare-feu", self.ask_ssh_from, when=lambda: self.uses("security_group")
              and "ssh" in self.st["sg_rules"]),
            s("Pare-feu", self.ask_ssh_cidr, when=lambda: self.uses("security_group")
              and "ssh" in self.st["sg_rules"] and self.st.get("ssh_from") == "custom"),
            s("Pare-feu", self.ask_ports, when=lambda: self.uses("security_group")),

            s("Clé SSH", self.ask_key_mode, when=lambda: self.uses("keypair")),
            s("Clé SSH", self.ask_key_pick, when=lambda: self.uses("keypair")
              and self.st["key_mode"] in ("file", "existing")),

            s("Machines virtuelles", self.ask_vm_count, when=lambda: self.uses("instances")),
            s("Machines virtuelles", self.ask_image, when=lambda: self.uses("instances")),
            s("Machines virtuelles", self.ask_flavor, when=lambda: self.uses("instances")),
            s("Machines virtuelles", self.ask_vm_names, when=lambda: self.uses("instances")),

            s("Accès depuis Internet", self.ask_access, when=lambda: self.uses("instances")
              and self.uses("floating_ip")),
            s("Accès depuis Internet", self.ask_public_extra, when=lambda: self.uses("instances")
              and self.uses("floating_ip") and self.st["access"] == "bastion"),
            s("Accès depuis Internet", self.ask_public_pick, when=lambda: self.uses("instances")
              and self.uses("floating_ip") and self.picks_public_machines),
            s("Accès depuis Internet", self.explain_internet, silent=True,
              when=lambda: self.uses("instances") and self.uses("floating_ip")),
            s("Accès depuis Internet", self.ask_internet, when=lambda: self.uses("instances")
              and self.can_proxy),
            s("Accès depuis Internet", self.ask_proxy_host, when=lambda: self.uses("instances")
              and self.proxy_wanted and not self.has_bastion and len(self.public) > 1),
            s("Accès depuis Internet", self.ask_nginx, when=lambda: self.uses("instances")
              and self.apt and bool(self.online)),

            s("Disques supplémentaires", self.ask_vol_count, when=lambda: self.uses("volumes")),
            s("Disques supplémentaires", self.ask_vol_size, when=lambda: self.uses("volumes")),
            s("Disques supplémentaires", self.ask_vol_type, when=lambda: self.uses("volumes")
              and self.found.volume_types),
            s("Disques supplémentaires", self.ask_vol_attach, when=lambda: self.uses("volumes")
              and self.workloads),

            s("Récapitulatif", self.review),
        ]

    # --- Moteur -----------------------------------------------------------------

    def run(self):
        ui.title("Nouveau projet",
                 "Réponds aux questions : felcloud écrit le code pour toi.\n"
                 "Échap revient à la question précédente, Ctrl+C quitte.")
        # Étapes posées : (indice, position du journal, section affichée avant elle).
        self.history = history = []
        self.shown_section = None
        self.done = False
        i = 0
        while not self.done:
            step = self.steps[i]
            if not step.when():
                i += 1
                continue
            position, shown = ui.mark(), self.shown_section
            try:
                if not step.silent and step.section != self.shown_section:
                    ui.section(step.section)
                    self.shown_section = step.section
                jump = step.ask()
            except ui.Back:
                if not history:
                    raise ui.Abort("Création annulée.") from None
                i, position, self.shown_section = history.pop()
                ui.rewind(position)
                continue
            if not step.silent:
                history.append((i, position, shown))
            if jump is not None:    # « Modifier » depuis le récapitulatif : indice de l'étape
                while history[-1][0] != jump:
                    history.pop()
                i, position, self.shown_section = history.pop()
                ui.rewind(position)
                continue
            i += 1
        return self.answers()

    # --- Réponses dérivées --------------------------------------------------------

    def uses(self, brick):
        bricks = self.st.get("bricks", [])
        if brick == "network":       # les machines ont toujours besoin d'un réseau
            return "network" in bricks or "instances" in bricks
        if brick == "instances":     # une IP publique s'attache à une machine
            return "instances" in bricks or "floating_ip" in bricks
        return brick in bricks

    @property
    def net_mode(self):
        return self.st.get("net_mode", "create") if self.found.project_networks else "create"

    @property
    def needs_external(self):
        return self.net_mode == "create" or self.uses("floating_ip")

    @property
    def names(self):
        count = int(self.st.get("vm_count", "1"))
        base = self.st.get("vm_base", "")
        return [base] if count == 1 else [f"{base}-{i}" for i in range(1, count + 1)]

    @property
    def bastion_name(self):
        return f"{self.st['project']}-bastion"

    @property
    def has_bastion(self):
        return self.uses("floating_ip") and self.st.get("access") == "bastion"

    @property
    def picks_public_machines(self):
        if self.st.get("access") == "direct":
            return len(self.names) > 1
        return self.st.get("public_extra", False)

    @property
    def public(self):
        if not self.uses("floating_ip"):
            return []
        if self.st.get("access") == "direct" and len(self.names) == 1:
            return list(self.names)
        if self.picks_public_machines:
            return [n for n in self.names if n in self.st.get("public_pick", [])]
        return []

    @property
    def private(self):
        return [n for n in self.names if n not in self.public]

    @property
    def apt(self):
        return cloud.is_apt_based(self.st.get("image", ""))

    @property
    def can_proxy(self):
        return bool(self.private) and bool(self.public or self.has_bastion) and self.apt

    @property
    def proxy_wanted(self):
        return self.can_proxy and self.st.get("internet") == "proxy"

    @property
    def proxy_host(self):
        if not self.proxy_wanted:
            return None
        if self.has_bastion:
            return self.bastion_name
        if len(self.public) == 1:
            return self.public[0]
        host = self.st.get("proxy_host")
        return host if host in self.public else self.public[0]

    @property
    def online(self):
        return set(self.public) | (set(self.private) if self.proxy_wanted else set())

    @property
    def workloads(self):
        return self.names if self.uses("instances") else []

    # --- Projet ---------------------------------------------------------------------

    def ask_name(self):
        self.st["project"] = ui.text("Nom du projet (il préfixera toutes les ressources) :",
                                     default=self.st.get("project", "mon-projet"),
                                     validate=_valid_project).strip()

    def ask_tool(self):
        choices = []
        for key, label in TOOLS.items():
            missing = missing_requirements(key)
            desc = TOOL_DESCRIPTIONS[key]
            if missing:
                desc += f" · à installer : {', '.join(sorted({m for m, _ in missing}))}"
            choices.append(ui.Choice(label, value=key, description=desc))
        ui.hint("Tous les outils créent exactement la même infrastructure.")
        tool = ui.select("Avec quel outil ?", choices, default=self.st.get("tool"))
        self.st["tool"] = tool
        missing = missing_requirements(tool)
        if missing:
            ui.warn(f"{TOOLS[tool]} a besoin de : {', '.join(sorted({m for m, _ in missing}))}. "
                    "Le projet sera écrit, mais il faudra l'installer avant de le lancer :")
            for how in sorted({h for _, h in missing}):
                ui.hint(how)

    def ask_cloud(self):
        default = self.st.get("cloud") or config.default_cloud()
        self.st["cloud"] = ui.select("Sur quel cloud ?", self.clouds, default=default)

    def discover(self):
        name = self.st.get("cloud") if len(self.clouds) > 1 else self.clouds[0]
        self.st["cloud"] = name
        if self.found_cloud != name:
            with ui.console.status(f"Lecture des ressources disponibles sur « {name} »..."):
                try:
                    conn = cloud.connect(name)
                    conn.authorize()
                    self.found = cloud.discover(conn)
                except Exception as exc:
                    raise ui.Abort(f"Connexion impossible : {cloud.explain(exc)}") from exc
            self.found_cloud = name
        ui.ok(f"Cloud « {name} » : {len(self.found.images)} images et "
              f"{len(self.found.flavors)} tailles de machine disponibles")

    def ask_bricks(self):
        previous = self.st.get("bricks")
        choices = [ui.Choice(label, value=key, description=desc,
                             checked=(key in previous) if previous is not None else checked)
                   for key, label, desc, checked in BRICKS]
        bricks = ui.checkbox("Que veux-tu créer ?", choices,
                             validate=lambda picked: True if picked else "Coche au moins un élément.")
        self.st["bricks"] = bricks
        if "floating_ip" in bricks and "instances" not in bricks:
            ui.info("Une IP publique s'attache à une machine : les machines virtuelles sont ajoutées.")
        if self.uses("instances") and "network" not in bricks:
            ui.info("Les machines ont besoin d'un réseau : il est ajouté.")
        if self.uses("instances") and "keypair" not in bricks:
            ui.warn("Sans clé SSH, tu ne pourras pas te connecter aux machines.")
        if self.uses("instances") and "floating_ip" not in bricks:
            ui.warn("Sans IP publique, les machines ne seront pas joignables depuis ton ordinateur "
                    "et n'auront pas accès à Internet.")

    # --- Réseau ---------------------------------------------------------------------

    def ask_net_mode(self):
        usable = [n for n in self.found.project_networks if n.get("router") or not self.uses("floating_ip")]
        self.st["net_mode"] = ui.select("Réseau des machines :", [
            ui.Choice("Créer un nouveau réseau pour ce projet (recommandé)", value="create",
                      description="réseau, sous-réseau et routeur dédiés, supprimés avec le projet"),
            ui.Choice("Utiliser un réseau existant", value="existing",
                      description="le projet dépendra d'un réseau et d'un routeur qu'il ne gère pas",
                      disabled=None if usable else "aucun n'est relié à un routeur vers Internet"),
        ], default=self.st.get("net_mode"))

    def ask_net_existing(self):
        choices = []
        for n in self.found.project_networks:
            unusable = self.uses("floating_ip") and not n.get("router")
            via = f"routeur {n['router']}" if n.get("router") else "aucun routeur vers Internet"
            choices.append(ui.Choice(n["name"], value=n["id"],
                                     description=f"{', '.join(n['cidrs'])} · {via}",
                                     disabled="pas d'IP publique possible" if unusable else None))
        net_id = ui.select("Lequel ?", choices, default=self.st.get("net_id"))
        self.st["net_id"] = net_id
        chosen = next(n for n in self.found.project_networks if n["id"] == net_id)
        ui.warn(f"Tes machines dépendront du routeur « {chosen.get('router') or 'aucun'} », qui n'est pas "
                "géré par ce projet : felcloud vérifiera après la création qu'elles sont joignables.")

    def ask_cidr(self):
        used = {c for n in self.found.project_networks for c in n["cidrs"]}
        default = next(f"10.0.{i}.0/24" for i in range(10, 250) if f"10.0.{i}.0/24" not in used)
        ui.hint("Adresses que recevront tes machines. Garde la valeur proposée : elle n'est pas "
                "déjà utilisée dans ton projet.")
        self.st["cidr"] = ui.text("Plage d'adresses privées :", default=self.st.get("cidr", default),
                                  validate=_valid_cidr).strip()

    def ask_external(self):
        choices = []
        for n in self.found.external_networks:
            recommended = n is self.found.external_networks[0]
            choices.append(ui.Choice(n["name"] + ("  (recommandé)" if recommended else ""), value=n["name"],
                                     description="accès Internet et IP publiques" if recommended
                                     else "réseau à usage interne"))
        self.st["external"] = ui.select("Réseau externe (sortie vers Internet) :",
                                        choices, default=self.st.get("external"))

    # --- Pare-feu --------------------------------------------------------------------

    def ask_sg_rules(self):
        ui.hint("Par défaut, tout le trafic entrant est bloqué. Coche ce que tu veux autoriser.")
        previous = self.st.get("sg_rules")
        rules = ui.checkbox("Autoriser :", [
            ui.Choice(label, value=key, description=desc,
                      checked=(key in previous) if previous is not None else checked)
            for key, label, desc, checked in SG_RULES])
        self.st["sg_rules"] = rules
        if self.uses("instances") and "ssh" not in rules:
            ui.warn("SSH non autorisé : tu ne pourras pas te connecter aux machines.")

    def ask_ssh_from(self):
        self.st["ssh_from"] = ui.select("SSH autorisé depuis :", [
            ui.Choice("N'importe où (0.0.0.0/0)", value="any",
                      description="simple ; la connexion reste protégée par ta clé SSH"),
            ui.Choice("Une adresse ou une plage précise", value="custom",
                      description="plus sûr : par exemple seulement l'adresse publique de ton bureau"),
        ], default=self.st.get("ssh_from", "any"))

    def ask_ssh_cidr(self):
        self.st["ssh_cidr"] = ui.text("Adresse ou plage autorisée (ex. 197.5.10.20/32) :",
                                      default=self.st.get("ssh_cidr", ""), validate=_valid_source).strip()

    def ask_ports(self):
        self.st["ports"] = ui.text("Autres ports TCP à ouvrir (ex. 8080,3000 — vide si aucun) :",
                                   default=self.st.get("ports", ""), validate=_valid_ports).strip()

    # --- Clé SSH ---------------------------------------------------------------------

    @property
    def key_name(self):
        name = f"{self.st['project']}-key"
        return f"{name}-2" if name in self.found.keypairs else name

    def ask_key_mode(self):
        key_path = Path.home() / ".ssh" / f"{self.st['project']}-key"
        choices = [ui.Choice("Créer une nouvelle clé pour ce projet (recommandé)", value="generate",
                             description=f"{_tilde(key_path)}, supprimée avec le projet")]
        if _public_keys():
            choices.append(ui.Choice("Utiliser une de mes clés", value="file",
                                     description="une clé déjà présente dans ~/.ssh sur cet ordinateur"))
        if self.found.keypairs:
            choices.append(ui.Choice("Utiliser une clé déjà enregistrée sur le cloud", value="existing",
                                     description="il te faudra la clé privée correspondante"))
        self.st["key_mode"] = ui.select("Quelle clé SSH utiliser ?", choices, default=self.st.get("key_mode"))
        if self.st["key_mode"] == "generate" and (key_path.exists() or key_path.with_suffix(".pub").exists()):
            ui.info(f"{_tilde(key_path)} existe déjà : elle sera réutilisée.")

    def ask_key_pick(self):
        if self.st["key_mode"] == "file":
            ui.hint("Seules les clés complètes (partie privée comprise) sont proposées.")
            pub = ui.select("Laquelle ?",
                            [ui.Choice(_tilde(p), value=str(p)) for p in _public_keys()],
                            default=self.st.get("key_file"))
            self.st["key_file"] = pub
        else:
            self.st["key_existing"] = ui.select("Laquelle ?", self.found.keypairs,
                                                default=self.st.get("key_existing"))
            ui.hint("Pour te connecter, il te faudra la clé privée correspondante sur ton ordinateur.")

    # --- Machines --------------------------------------------------------------------

    def ask_vm_count(self):
        self.st["vm_count"] = ui.text("Combien de machines ?", default=self.st.get("vm_count", "1"),
                                      validate=_valid_int(1, 20)).strip()

    def ask_image(self):
        choices = [ui.Choice(name + ("  (recommandé)" if i == 0 else ""), value=name)
                   for i, name in enumerate(self.found.images)]
        self.st["image"] = ui.select("Système (image) :", choices, default=self.st.get("image"))

    def ask_flavor(self):
        recommended = _recommended_flavor(self.found.flavors)
        choices = [ui.Choice(f.label + ("  (conseillé pour débuter)" if f is recommended else ""), value=f.name)
                   for f in self.found.flavors]
        self.st["flavor"] = ui.select("Taille de chaque machine (flavor) :", choices,
                                      default=self.st.get("flavor", recommended.name))

    def ask_vm_names(self):
        project = self.st["project"]
        default = self.st.get("vm_base", f"{project}-vm")
        if int(self.st["vm_count"]) == 1:
            base = ui.text("Nom de la machine :", default=default, validate=self._valid_base)
        else:
            ui.hint(f"Les machines seront numérotées : {default}-1, {default}-2...")
            base = ui.text("Début du nom des machines :", default=default, validate=self._valid_base)
        self.st["vm_base"] = base.strip()

    def _valid_base(self, value):
        valid = _valid_vm_name(value)
        if valid is not True:
            return valid
        if value.strip() == self.bastion_name:
            return f"« {self.bastion_name} » est réservé au bastion : choisis un autre nom."
        return True

    # --- Accès depuis Internet ------------------------------------------------------

    def ask_access(self):
        count = len(self.names)
        bastion = ui.Choice(
            f"Par un bastion : une petite machine en plus ({self.bastion_name})", value="bastion",
            description="seul le bastion a une IP publique (porte d'entrée) ; tes machines restent privées")
        direct = ui.Choice(
            "Directement : une IP publique sur ta machine" if count == 1
            else "Directement : une IP publique sur les machines de ton choix", value="direct",
            description="pas de machine en plus : chaque machine publique est joignable depuis Internet")
        # Bastion conseillé dès 2 machines ; pour une seule, l'IP directe suffit.
        if count == 1:
            choices = [ui.Choice(direct.title + "  (recommandé)", value="direct", description=direct.description),
                       bastion]
        else:
            choices = [ui.Choice(bastion.title + "  (recommandé)", value="bastion",
                                 description=bastion.description), direct]
        ui.hint("Sur FelCloud, une machine n'accède à Internet que si elle a une IP publique, "
                "ou en passant par une machine qui en a une.")
        self.st["access"] = ui.select("Comment accéder à tes machines depuis Internet ?",
                                      choices, default=self.st.get("access"))
        if self.st["access"] == "bastion":
            b = self.bastion()
            ui.info(f"Bastion ajouté : {b['name']} · {b['flavor']} · {b['image']}")

    def bastion(self):
        # Le bastion n'a besoin que de peu de ressources : la plus petite taille suffit.
        # Il doit tourner sous Ubuntu/Debian pour pouvoir héberger le proxy.
        flavor = min(self.found.flavors, key=lambda f: (f.vcpus, f.ram_mb, f.disk_gb))
        image = self.st["image"]
        if not cloud.is_apt_based(image):
            image = next((i for i in self.found.images if cloud.is_apt_based(i)), image)
        return {"name": self.bastion_name, "image": image, "flavor": flavor.name, "floating_ip": True,
                "nginx": False, "ssh_user": cloud.ssh_user_for(image), "role": "bastion"}

    def ask_public_extra(self):
        self.st["public_extra"] = ui.select("Une machine doit-elle AUSSI avoir sa propre IP publique ?", [
            ui.Choice("Non, seul le bastion est public (recommandé)", value=False),
            ui.Choice("Oui, certaines doivent être joignables directement", value=True,
                      description="par exemple un serveur web ouvert au public"),
        ], default=self.st.get("public_extra", False))

    def ask_public_pick(self):
        previous = self.st.get("public_pick")
        names = self.names
        picked = ui.checkbox(
            "Quelles machines reçoivent une IP publique ?",
            [ui.Choice(n, value=n, checked=(n in previous) if previous is not None else i == 0)
             for i, n in enumerate(names)],
            validate=lambda p: True if p else "Coche au moins une machine.")
        self.st["public_pick"] = picked

    def explain_internet(self):
        """Avertit quand des machines privées ne pourront pas accéder à Internet."""
        if not self.private:
            return
        if not (self.public or self.has_bastion):
            ui.warn("Aucune machine n'a d'IP publique : elles n'auront pas accès à Internet "
                    "(pas d'installation de paquets avec apt, pip...).")
        elif not self.apt:
            ui.warn("Le proxy automatique n'existe que pour Ubuntu et Debian : "
                    "les machines sans IP publique n'auront pas accès à Internet.")

    def ask_internet(self):
        host = self.bastion_name if self.has_bastion else "une machine publique"
        if not self.has_bastion and len(self.public) == 1:
            host = self.public[0]
        count = len(self.private)
        question = ("Accès à Internet pour la machine sans IP publique ?" if count == 1
                    else f"Accès à Internet pour les {count} machines sans IP publique ?")
        self.st["internet"] = ui.select(question, [
            ui.Choice(f"Oui, via un proxy installé sur {host} (recommandé)", value="proxy",
                      description="pour installer des paquets (apt, pip...) ; installé automatiquement"),
            ui.Choice("Non, pas d'accès à Internet", value="none",
                      description="les machines privées ne pourront rien télécharger"),
        ], default=self.st.get("internet", "proxy"))

    def ask_proxy_host(self):
        self.st["proxy_host"] = ui.select(
            "Quelle machine sert de proxy et de rebond SSH ?",
            self.public, default=self.st.get("proxy_host"))

    def ask_nginx(self):
        self.st["nginx"] = ui.confirm("Installer un serveur web (nginx) au démarrage des machines ?",
                                      default=self.st.get("nginx", False))
        if self.st["nginx"] and len(self.online) < len(self.names):
            ui.warn("nginx ne sera installé que sur les machines qui ont accès à Internet.")
        rules = self.st.get("sg_rules", [])
        if self.st["nginx"] and self.public and self.uses("security_group") and "http" not in rules:
            if ui.confirm("Ouvrir le port 80 dans le pare-feu pour voir le site ?",
                          default=True):
                self.st["sg_rules"] = [key for key, *_ in SG_RULES if key in rules + ["http"]]
                ui.ok("Port 80 (HTTP) ajouté au pare-feu.")
        elif self.st["nginx"] and not self.public:
            ui.hint("Tes machines n'ont pas d'IP publique : le site ne sera visible que depuis le réseau "
                    "du projet (par exemple avec curl depuis le bastion).")

    # --- Disques -----------------------------------------------------------------------

    def ask_vol_count(self):
        self.st["vol_count"] = ui.text("Combien de disques ?", default=self.st.get("vol_count", "1"),
                                       validate=_valid_int(1, 10)).strip()

    def ask_vol_size(self):
        self.st["vol_size"] = ui.text("Taille de chaque disque (Go) :", default=self.st.get("vol_size", "10"),
                                      validate=_valid_int(1, 2000)).strip()

    def ask_vol_type(self):
        def label(t):
            return {"nvme": "rapide (SSD)", "hdd": "économique, plus lent"}.get(t.lower())

        self.st["vol_type"] = ui.select("Type de stockage :", [
            ui.Choice(t, value=t, description=label(t)) for t in self.found.volume_types],
            default=self.st.get("vol_type"))

    @property
    def volume_names(self):
        count = int(self.st.get("vol_count", "1"))
        project = self.st["project"]
        return [f"{project}-data"] if count == 1 else [f"{project}-data-{i}" for i in range(1, count + 1)]

    def ask_vol_attach(self):
        previous = self.st.get("vol_attach", {})
        attach = {}
        for i, name in enumerate(self.volume_names):
            options = [ui.Choice(vm, value=vm) for vm in self.workloads]
            options.append(ui.Choice("Ne pas l'attacher pour l'instant", value=""))
            default = previous.get(name, self.workloads[i % len(self.workloads)])
            attach[name] = ui.select(f"Attacher « {name} » à :", options, default=default)
        self.st["vol_attach"] = attach

    # --- Récapitulatif ----------------------------------------------------------------

    def review(self):
        answers = self.answers()
        summary(answers)
        quota_ok = check_quotas(answers, self.found)
        first_step = {}         # section → indice de sa première question posée
        for index, _, _ in self.history:
            first_step.setdefault(self.steps[index].section, index)
        choices = [ui.Choice("Générer le projet" if quota_ok else "Générer quand même le projet", value="go")]
        choices += [ui.Choice(f"Modifier : {section}", value=index) for section, index in first_step.items()]
        choices.append(ui.Choice("Annuler", value="cancel"))
        action = ui.select("Tout est correct ?", choices)
        if action == "cancel":
            raise ui.Abort("Création annulée.")
        if action == "go":
            self.done = True
            return None
        return action           # indice de l'étape où reprendre

    def answers(self):
        """Transforme les réponses brutes en dictionnaire pour les templates (felcloud.yaml)."""
        st = self.st
        a = {"version": 1, "project": st["project"], "tool": st["tool"], "cloud": st["cloud"]}

        if self.uses("network"):
            externals = self.found.external_networks
            external = st.get("external") if len(externals) > 1 else (externals[0]["name"] if externals else "")
            if self.net_mode == "existing":
                chosen = next(n for n in self.found.project_networks if n["id"] == st["net_id"])
                net = {"mode": "existing", "name": chosen["name"], "id": chosen["id"]}
            else:
                net = {"mode": "create", "cidr": st["cidr"], "dns": ["1.1.1.1", "8.8.8.8"]}
            if self.needs_external:
                if not external:
                    raise ui.Abort("Aucun réseau externe n'est visible sur ce projet : "
                                   "impossible d'accéder à Internet.")
                net["external_network"] = external
            a["network"] = net

        if self.uses("security_group"):
            ssh_custom = "ssh" in st["sg_rules"] and st.get("ssh_from") == "custom"
            a["security_group"] = {
                "rules": st["sg_rules"],
                "ssh_cidr": st["ssh_cidr"] if ssh_custom else "0.0.0.0/0",
                "custom_tcp_ports": [int(p) for p in st.get("ports", "").replace(" ", "").split(",") if p],
            }

        if self.uses("keypair"):
            mode = st["key_mode"]
            if mode == "existing":
                a["keypair"] = {"mode": "existing", "name": st["key_existing"], "private_key_path": ""}
            elif mode == "file":
                pub = Path(st["key_file"])
                a["keypair"] = {"mode": "file", "name": self.key_name, "public_key_path": _tilde(pub),
                                "private_key_path": _tilde(pub.with_suffix(""))}
            else:
                key_path = Path.home() / ".ssh" / f"{st['project']}-key"
                a["keypair"] = {"mode": "generate", "name": self.key_name,
                                "public_key_path": _tilde(key_path) + ".pub",
                                "private_key_path": _tilde(key_path)}

        if self.uses("instances"):
            image = st["image"]
            user = cloud.ssh_user_for(image)
            nginx = self.apt and bool(self.online) and st.get("nginx", False)
            vms = [{"name": n, "image": image, "flavor": st["flavor"], "floating_ip": n in self.public,
                    "nginx": nginx and n in self.online, "ssh_user": user, "role": ""} for n in self.names]
            if self.has_bastion:
                vms.insert(0, self.bastion())
            a["instances"] = vms
            a["internet_proxy"] = {"bastion": self.proxy_host} if self.proxy_host else None

        if self.uses("volumes"):
            attach = st.get("vol_attach", {})
            a["volumes"] = [{"name": name, "size": int(st["vol_size"]), "type": st.get("vol_type", ""),
                             "attach_to": attach.get(name) or None} for name in self.volume_names]
        return a


def check_quotas(answers, found):
    """Avertit si le projet n'a pas assez de quota. Retourne False en cas de dépassement certain."""
    flavors = {f.name: f for f in found.flavors}
    vms = answers.get("instances") or []
    needs = {
        "instances": len(vms),
        "cores": sum(flavors[v["flavor"]].vcpus for v in vms),
        "ram_mb": sum(flavors[v["flavor"]].ram_mb for v in vms),
        "floating_ips": sum(v["floating_ip"] for v in vms),
        "volumes": len(answers.get("volumes") or []),
        "volume_gb": sum(v["size"] for v in answers.get("volumes") or []),
    }
    labels = {"instances": "machines", "cores": "vCPU", "ram_mb": "Mo de RAM",
              "floating_ips": "IP publiques", "volumes": "disques", "volume_gb": "Go de disques"}
    ok = True
    for key, need in needs.items():
        left = _remaining(found.limits, key)
        if need and left is not None and need > left:
            ui.err(f"Quota insuffisant : {need} {labels[key]} demandés, {left} disponibles sur le projet.")
            ok = False
    return ok


def summary(answers):
    table = Table(show_header=False)
    table.add_column(style="bold")
    table.add_column()
    table.add_row("Projet", f"{answers['project']}  ({TOOLS[answers['tool']]}, cloud {answers['cloud']})")
    net = answers.get("network")
    if net:
        table.add_row("Réseau", f"nouveau, {net['cidr']}" + (f", sortie via {net['external_network']}"
                                                            if net.get("external_network") else "")
                      if net["mode"] == "create" else f"existant : {net['name']}")
    sg = answers.get("security_group")
    if sg:
        rules = [label for key, label, _, _ in SG_RULES if key in sg["rules"]]
        if "ssh" in sg["rules"] and sg["ssh_cidr"] != "0.0.0.0/0":
            rules = [f"SSH depuis {sg['ssh_cidr']}" if r.startswith("SSH") else r for r in rules]
        rules += [f"TCP {p}" for p in sg["custom_tcp_ports"]]
        table.add_row("Pare-feu", ", ".join(rules) or "aucune ouverture")
    kp = answers.get("keypair")
    if kp:
        table.add_row("Clé SSH", {"generate": f"nouvelle : {kp['private_key_path']}",
                                  "file": f"{kp['public_key_path']}",
                                  "existing": f"existante sur le cloud : {kp['name']}"}[kp["mode"]])
    proxy = answers.get("internet_proxy")
    for vm in answers.get("instances") or []:
        extra = " + IP publique" if vm["floating_ip"] else ""
        if vm.get("role") == "bastion":
            extra += " (bastion : porte d'entrée SSH)"
        if proxy and vm["name"] == proxy["bastion"]:
            extra += " + proxy pour les autres"
        elif proxy and not vm["floating_ip"]:
            extra += " (Internet via le proxy)"
        elif not vm["floating_ip"]:
            extra += " (sans Internet)"
        extra += " + nginx" if vm["nginx"] else ""
        table.add_row("Machine", f"{vm['name']} · {vm['flavor']} · {vm['image']}{extra}")
    for vol in answers.get("volumes") or []:
        where = f" → {vol['attach_to']}" if vol["attach_to"] else " (non attaché)"
        table.add_row("Disque", f"{vol['name']} · {vol['size']} Go {vol['type']}{where}")
    ui.show(table)


def run():
    """Pose toutes les questions et retourne le dictionnaire de réponses."""
    return Wizard().run()
