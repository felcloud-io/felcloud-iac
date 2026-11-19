"""Affichage et questions interactives (menus aux flèches).

Toutes les questions s'utilisent de la même façon :
  ↑ ↓      se déplacer        Espace   cocher / décocher (listes à cocher)
  Entrée   valider            Échap    revenir à la question précédente

Une fois la réponse donnée, le menu disparaît et laisse une ligne
« ? Question  réponse », en français. Tout ce qui est affiché est aussi noté dans
un journal : en cas de retour en arrière, l'écran est redessiné tel qu'il était
au moment de la question, sans les réponses annulées.
"""

import re

import questionary
from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings
from prompt_toolkit.keys import Keys
from questionary import Choice, Separator, Style
from questionary.prompts.common import InquirerControl
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel

console = Console(highlight=False)

BLUE = "#2e86de"
GREEN = "#27ae60"

STYLE = Style([
    ("qmark", f"fg:{BLUE} bold"),
    ("question", "bold"),
    ("pointer", f"fg:{BLUE} bold"),
    # Ligne sous le curseur.
    ("highlighted", f"fg:{BLUE} bold"),
    # Élément coché. « noreverse » annule l'inversion appliquée par défaut par
    # prompt_toolkit, qui affichait les éléments cochés sur fond vert.
    ("selected", f"fg:{GREEN} noreverse"),
    ("text", ""),
    ("disabled", "fg:#858585 italic"),
    ("separator", "fg:#858585"),
    ("answer", f"fg:{GREEN} bold"),
    ("instruction", "fg:#7f8c8d italic"),
    ("validation-toolbar", "fg:#ffffff bg:#c0392b"),
])

SELECT_HINT = "(↑↓ puis Entrée · Échap : retour)"
CHECK_HINT = "(↑↓ · Espace pour cocher · Entrée pour valider · Échap : retour)"


class Abort(Exception):
    """L'utilisateur a quitté (Ctrl+C), ou l'opération ne peut pas continuer."""


class Back(Abort):
    """L'utilisateur a appuyé sur Échap pour revenir à la question précédente.

    Hors de l'assistant, qui sait revenir en arrière, cela revient à annuler."""


# --- Journal de l'écran -------------------------------------------------------

_journal = []


def _show(renderable):
    _journal.append(renderable)
    console.print(renderable)


def mark():
    """Position actuelle du journal, à passer à rewind()."""
    return len(_journal)


def rewind(position):
    """Efface l'écran et le réaffiche tel qu'il était à `position`."""
    del _journal[position:]
    console.clear()
    for renderable in _journal:
        console.print(renderable)


# --- Questions -----------------------------------------------------------------

def _back_on_escape():
    keys = KeyBindings()

    @keys.add(Keys.Escape, eager=True)
    def _(event):
        event.app.exit(exception=Back())

    return keys


def _french_descriptions(question):
    """questionary affiche « Description: ... » sous l'option pointée : on le remplace
    par une ligne grise, en français."""
    for control in question.application.layout.find_all_controls():
        if isinstance(control, InquirerControl):
            tokens = control.text

            def translated(tokens=tokens):
                return [("class:instruction", "  ↳ " + text[len("  Description: "):])
                        if text.startswith("  Description: ") else (style, text)
                        for style, text in tokens()]

            control.text = translated


def _ask(question, message, shown):
    """Pose la question, puis remplace le menu par la ligne « ? message  réponse ».

    `shown(réponse)` donne le texte de la réponse à afficher."""
    app = question.application
    app.erase_when_done = True
    app.key_bindings = merge_key_bindings([app.key_bindings or KeyBindings(), _back_on_escape()])
    _french_descriptions(question)
    try:
        answer = question.unsafe_ask()
    except KeyboardInterrupt:
        raise Abort from None
    # Les espaces d'alignement des listes (tailles de machine...) sont inutiles ici.
    shown_text = re.sub(r" {3,}", "  ", shown(answer))
    _show(f"[bold {BLUE}]?[/] [bold]{escape(message)}[/] [bold {GREEN}]{escape(shown_text)}[/]")
    return answer


def _title_of(choices, value):
    for choice in choices:
        if isinstance(choice, Choice) and choice.value == value:
            return choice.title if isinstance(choice.title, str) else value
        if choice == value:
            return str(choice)
    return str(value)


def select(message, choices, default=None):
    """Une réponse parmi une liste. `default` : valeur pointée au départ."""
    values = [c.value if isinstance(c, Choice) else c for c in choices
              if not isinstance(c, Separator) and not (isinstance(c, Choice) and c.disabled)]
    question = questionary.select(
        message, choices=choices, default=default if default in values else None, style=STYLE,
        instruction=SELECT_HINT, show_description=True,
    )
    return _ask(question, message, lambda answer: _title_of(choices, answer))


def checkbox(message, choices, validate=None):
    """Plusieurs réponses. Les choix cochés au départ ont `checked=True`."""
    question = questionary.checkbox(
        message, choices=choices, style=STYLE, instruction=CHECK_HINT, show_description=True,
        validate=validate or (lambda _: True),
    )
    return _ask(question, message,
                lambda answer: ", ".join(_title_of(choices, a) for a in answer) or "aucun")


def confirm(message, default=True):
    """Oui ou Non, choisi aux flèches comme les autres questions."""
    return select(message, [Choice("Oui", value=True), Choice("Non", value=False)], default=default)


def text(message, default="", validate=None):
    question = questionary.text(message, default=default, style=STYLE,
                                validate=validate or (lambda _: True))
    return _ask(question, message, lambda answer: answer.strip() or "(vide)")


def password(message, validate=None):
    question = questionary.password(message, style=STYLE, validate=validate or (lambda _: True))
    return _ask(question, message, lambda answer: "•" * 8)


def path(message, default="", only_directories=False, validate=None):
    question = questionary.path(message, default=default, only_directories=only_directories,
                                validate=validate, style=STYLE)
    return _ask(question, message, lambda answer: answer)


# --- Messages ------------------------------------------------------------------

def title(text_, subtitle=None):
    body = f"[bold]{text_}[/]"
    if subtitle:
        body += f"\n[dim]{subtitle}[/]"
    _show("")
    _show(Panel(body, border_style=BLUE, expand=False, padding=(0, 2)))


def section(text_):
    _show(f"\n[bold {BLUE}]── {text_} ──[/]")


def ok(msg):
    _show(f"[green]✓[/] {msg}")


def warn(msg):
    _show(f"[yellow]![/] {msg}")


def err(msg):
    _show(f"[red]✗[/] {msg}")


def info(msg):
    _show(f"[cyan]›[/] {msg}")


def hint(msg):
    _show(f"  [dim]{msg}[/]")


def show(renderable):
    """Affiche un tableau ou un panneau en le notant dans le journal."""
    _show(renderable)


__all__ = [
    "Abort", "Back", "Choice", "Panel", "Separator", "console", "select", "checkbox", "confirm",
    "text", "password", "path", "title", "section", "ok", "warn", "err", "info", "hint", "show",
    "mark", "rewind",
]
