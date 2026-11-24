"""Council of Achrona: een mini-Council met Raadsleden en een Koning.

Start met `uv run council`. Schrijft verdict.json en trace.json in de root van de repo.
De Koning leidt het debat in `kies_volgende()`: hij beslist wie spreekt, met welke vraag en waarom.
"""
import asyncio
import copy
import json
import os
import sys
import uuid
from contextlib import AsyncExitStack
from datetime import datetime
from pathlib import Path

from agent_framework import Agent, MCPStreamableHTTPTool
from agent_framework.openai import OpenAIChatCompletionClient
from dotenv import load_dotenv
from openai import AsyncOpenAI

import raadsleden
from koning import Koning

ROOT = Path(__file__).parent
KENNISBRONNEN = ROOT / "kennisbronnen"
ROLLEN = ("archmage", "engineer", "guardian_keeper", "historian", "oracle", "beast_master")
BEURTENBUDGET = 10  # spelregel: maximaal 10 Raadslid-beurten (berichten van Raadsleden) per run


def kies_volgende(beurten, leden, koning):
    """De Koning leidt het debat: hij beslist wie spreekt. De kit roept deze functie op vóór elke beurt.
    (In AI-termen heet dit deel een orchestrator.)

    Je krijgt:
      beurten  het debat tot nu toe, dezelfde lijst als in trace.json
      leden    de Raadsleden uit raadsleden/, elk met .ROL, .NAAM, .PERSPECTIEF, .BELANG, .NEIGING, .BLINDE_VLEK
      koning   de Koning als agent (koning.py), met zijn logboek

    Je geeft terug:
      (rol, vraag, reden, open_vragen)   wie nu spreekt, welke vraag, waarom, en wat nog open staat
      (None, None, reden, open_vragen)   de Koning stopt het debat zelf, met de reden waarom
    De reden komt in de trace en in de viewer. De jury leest ze: daarom vraagt koning.py een vast
    formaat (doel, waarom deze Rol, verwacht) en weigert het vage redenen.
    """
    gesproken = sum(1 for b in beurten if b["soort"] == "bericht" and b["spreker"] in ROLLEN)
    return koning.kies(beurten, BEURTENBUDGET - gesproken)


def controleer(leden):
    """Duidelijke melding in plaats van een traceback bij de meest voorkomende fouten."""
    mist = [v for v in ("OPENAI_BASE_URL", "OPENAI_API_KEY", "MODEL_KLEIN", "MODEL_GROOT") if not os.getenv(v)]
    if mist:
        sys.exit(f"In .env ontbreekt: {', '.join(mist)}. Kopieer .env.example naar .env en vul het in, of zie 'Vooraf installeren' in de README.")
    for lid in leden:
        if lid.ROL not in ROLLEN:
            sys.exit(f"raadsleden/: ROL = {lid.ROL!r} is ongeldig. Gebruik de mapnaam van de Rol: {', '.join(ROLLEN)}.")
    dubbel = {l.ROL for l in leden if [x.ROL for x in leden].count(l.ROL) > 1}
    if dubbel:
        sys.exit(f"Eén Raadslid per Rol: {', '.join(dubbel)} komt meer dan eens voor in raadsleden/.")


def client(model_env):
    key = os.environ["OPENAI_API_KEY"]
    return OpenAIChatCompletionClient(
        model=os.environ[model_env],
        async_client=AsyncOpenAI(
            base_url=os.environ["OPENAI_BASE_URL"],
            api_key=key,
            max_retries=8,  # bij een rate limit (429) wacht de client en probeert opnieuw
        ),
    )


def kennisbron_bestanden(rol):
    map_ = KENNISBRONNEN / rol
    return sorted(p for p in map_.glob("*") if p.suffix in (".md", ".csv")) if map_.is_dir() else []


def instructies(lid, bestanden, mcp):
    # Alleen gedragsregels: geen inhoud uit de Kennisbronnen (spelregel).
    tekst = (
        f"Je bent de {lid.NAAM}, een Raadslid in de Council of Achrona. {lid.PERSPECTIEF}\n"
        "Je debatteert met de andere Raadsleden over de oorzaak van de Desync. De Koning leidt het debat en stelt je "
        "telkens een gerichte vraag. Er zijn maar 10 Raadslid-beurten voor de hele Council: maak je beurt waardevol.\n\n"
        "Zo antwoord je:\n"
        "1. Baseer je alleen op je eigen Kennisbron. Noem bij elke bewering de record-ID's precies zoals ze in je "
        "Kennisbron staan, met een gewoon koppelteken (bv. ABC-123). Verzin nooit een ID of feit.\n"
        "2. Beantwoord elk deel van de vraag van de Koning, punt per punt.\n"
        "3. Reageer op beweringen van andere Raadsleden die je bron raakt: bevestig of weerleg ze met je eigen ID's. "
        "Zeg het expliciet als je het oneens bent.\n"
        "4. Wees eerlijk over de grenzen van je bron: wat kan ze niet aantonen, en kan ze je eigen domein uitsluiten "
        "als oorzaak? Weet je iets niet, zeg dat.\n"
        "5. Verwijst je bron naar een andere Rol? Meld naar wie en waarover.\n"
        "6. Herhaal of vat niet samen wat al gezegd is: alleen nieuwe informatie, bevestiging of weerlegging. "
        "Hoogstens ongeveer 200 woorden."
    )
    if mcp:
        tekst += ("\n\nDoorzoek je Kennisbron grondig met je tools voor je antwoordt: zoeken kost geen beurt, "
                  "alleen je antwoord telt.")
    elif bestanden:
        tekst += "\n\nJe Kennisbron:\n\n" + "\n\n".join(f"## {p.name}\n\n{p.read_text(encoding='utf-8')}" for p in bestanden)
    else:
        tekst += "\nJe hebt nog geen Kennisbron: zeg dat je niets kan bewijzen."
    return tekst


def tel(verbruik, model, usage):
    if not usage:
        return
    v = verbruik.setdefault(model, {"beurten": 0, "invoer": 0, "gecached": 0, "uitvoer": 0})
    v["beurten"] += 1  # één beurt = één agent-run (met MCP soms meerdere model-requests)
    v["invoer"] += usage.get("input_token_count") or 0
    v["gecached"] += usage.get("cache_read_input_token_count") or 0
    v["uitvoer"] += usage.get("output_token_count") or 0


def toolcalls(response):
    for msg in response.messages:
        for c in msg.contents:
            if c.type == "function_call":
                args = c.arguments
                yield c.name, json.loads(args) if isinstance(args, str) and args else (args or {})


async def run():
    load_dotenv(ROOT / ".env")
    opties = {"reasoning_effort": os.environ["REASONING"]} if os.getenv("REASONING") else None  # minder denkwerk = minder tokens
    run_id = f"{datetime.now().astimezone().isoformat(timespec='seconds')}_{uuid.uuid4().hex[:8]}"
    leden = raadsleden.alle()
    controleer(leden)
    beurten, verbruik = [], {}
    trace = {"run_id": run_id, "status": "bezig", "raadsleden": [l.ROL for l in leden], "beurten": beurten, "geschillen": [], "verbruik": verbruik}

    def bewaar(naam, data):
        (ROOT / naam).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def beurt(ronde, spreker, soort, **velden):
        beurten.append({"nr": len(beurten) + 1, "ronde": ronde, "spreker": spreker, "soort": soort, **velden})
        bewaar("trace.json", trace)  # na elke beurt: de viewer (`uv run viewer`) toont het debat live

    klein = client("MODEL_KLEIN")
    koning = Koning(leden)  # leidt het debat; leest geen Kennisbron

    async with AsyncExitStack() as stack:
        agents = []
        for lid in leden:
            mcp_url = os.getenv(f"MCP_{lid.ROL.upper()}", "").strip()
            bestanden = [] if mcp_url else kennisbron_bestanden(lid.ROL)
            tools = []
            if mcp_url:
                try:
                    tools.append(await stack.enter_async_context(MCPStreamableHTTPTool(name=f"kennisbron_{lid.ROL}", url=mcp_url)))
                except Exception as e:
                    sys.exit(f"MCP voor {lid.NAAM} ({mcp_url}) is onbereikbaar: {e}\n"
                             f"Is de server al open (vanaf 10:00)? Klopt het pad /{lid.ROL}/mcp? "
                             f"Of laat MCP_{lid.ROL.upper()} leeg om de bestanden uit kennisbronnen/{lid.ROL}/ te gebruiken.")
            elif not bestanden:
                print(f"LET OP: {lid.NAAM} heeft geen Kennisbron (MCP_{lid.ROL.upper()} is leeg en kennisbronnen/{lid.ROL}/ ook).")
            for p in bestanden:  # fallback zonder MCP: welke bestanden gingen in de prompt
                beurt(0, lid.ROL, "bestand", bestand=f"{lid.ROL}/{p.name}")
            agents.append((lid, mcp_url, Agent(klein, instructies(lid, bestanden, mcp_url), name=lid.ROL, tools=tools)))

        (ROOT / "verdict.json").unlink(missing_ok=True)  # setup gelukt: geen Verdict van een vorige run laten staan
        bewaar("trace.json", trace)
        per_rol = {lid.ROL: (lid, mcp_url, agent) for lid, mcp_url, agent in agents}
        debat, ronde = [], 1
        while True:
            gesproken = sum(1 for b in beurten if b["soort"] == "bericht" and b["spreker"] in per_rol)
            if gesproken >= BEURTENBUDGET:
                rol, vraag, reden, open_vragen = None, None, f"Beurtenbudget op ({BEURTENBUDGET} Raadslid-beurten)", koning.open_vragen
            else:
                trace["aan_het_woord"] = "koning"  # de viewer toont dat de Koning nadenkt
                bewaar("trace.json", trace)
                # In een aparte thread: de LLM-call van de Koning blokkeert de MCP-verbindingen niet.
                rol, vraag, reden, open_vragen = await asyncio.to_thread(kies_volgende, beurten, leden, koning)
                for u in koning.neem_verbruik():
                    tel(verbruik, "groot", u)
            if rol is not None and rol not in per_rol:
                sys.exit(f"kies_volgende() koos {rol!r}, maar die Rol zit niet in raadsleden/.")
            if rol is not None:
                ronde = gesproken // len(leden) + 1
            # Elke keuze van de Koning staat in de trace: aan wie, welke vraag, waarom, wat nog open staat
            # en zijn logboek (extra veld, mag volgens het schema).
            beurt(ronde, "koning", "routering", aan=rol, vraag=vraag, reden=reden,
                  open_vragen=open_vragen, logboek=copy.deepcopy(koning.logboek), prompt=koning.prompt_naam)
            print(f"\n[Koning] {'stopt' if rol is None else '→ ' + per_rol[rol][0].NAAM}: {reden}")
            if rol is None:
                break
            lid, mcp_url, agent = per_rol[rol]
            # Wie ziet wat? Hier ziet elk Raadslid het hele debat (broadcast). Ook dat is een keuze.
            prompt = "Debat tot nu toe:\n\n" + ("\n\n".join(debat) or "(nog niets)") + f"\n\nVraag aan jou: {vraag}"
            trace["aan_het_woord"] = rol
            bewaar("trace.json", trace)
            r = await agent.run(prompt, options=opties)
            tel(verbruik, "klein", r.usage_details)
            for tool, args in toolcalls(r):
                beurt(ronde, rol, "toolcall", tool=tool, argumenten=args, pad=mcp_url)
            beurt(ronde, rol, "bericht", tekst=r.text)
            debat.append(f"{lid.NAAM}: {r.text}")
            print(f"\n[ronde {ronde}] {lid.NAAM}:\n{r.text}")

    # De Koning heeft geen Rol en geen Kennisbron: hij kent alleen wat de Raadsleden inbrachten.
    trace["aan_het_woord"] = "koning"
    bewaar("trace.json", trace)
    # De Koning weegt af en stelt het Verdict op uit het debat (koning.py): alleen ID's die een Raadslid
    # echt noemde, gewone koppeltekens, en getoetst aan de schema's voor het wordt weggeschreven.
    verdict, geschillen, afweging, slottekst = await asyncio.to_thread(koning.verdict, beurten, run_id)
    for u in koning.neem_verbruik():
        tel(verbruik, "groot", u)
    trace["geschillen"] = geschillen
    trace["afweging"] = afweging  # extra veld (mag volgens het schema): hoe de Koning per slot woog
    beurt(ronde + 1, "koning", "bericht", tekst=slottekst)
    print(f"\n[Koning]:\n{slottekst}")
    trace["status"], trace["aan_het_woord"] = "klaar", None
    bewaar("trace.json", trace)
    bewaar("verdict.json", verdict)

    print(f"\nKlaar: verdict.json en trace.json geschreven (run_id {run_id}).")
    for model, v in verbruik.items():
        print(f"Verbruik {model}: {v['beurten']} beurten, {v['invoer']} tokens in ({v['gecached']} gecached), {v['uitvoer']} tokens uit")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(run())


if __name__ == "__main__":
    main()
