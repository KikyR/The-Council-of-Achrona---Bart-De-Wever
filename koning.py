"""De Koning van de Council of Achrona: leidt het debat als agent.

Hij leest geen Kennisbron (geen tools). Hij kent alleen de publieke Rolkaarten en wat de Raadsleden
in het debat zeggen. Na elke beurt werkt hij zijn logboek bij en kiest hij wie spreekt, met welke
vraag en waarom. Zijn prompts staan in prompts/prompt<n>.py (zie laad_prompt()).
Spelregel: die prompts bevatten geen inhoud uit de Kennisbronnen (geen ID's of feiten).
"""
import copy
import importlib.util
import json
import os
import re
from pathlib import Path

from jsonschema import Draft202012Validator
from openai import BadRequestError, OpenAI

SLOTS = ("ingreep", "mechanisme", "verloop")
# Een concrete reden noemt waar de beurt voor dient: een slot, een open vraag, een geschil, herstel of risico.
REDEN_DOELEN = SLOTS + ("open vraag", "geschil", "herstel", "risico", "dwaalspoor", "dwaalsporen")
DWAALSPOOR_SOORTEN = ("liegende_rol", "onmogelijk", "tegenstrijdig", "ander")
VAGE_REDENEN = ("meer info", "meer informatie", "volgende in de rij", "vaste volgorde", "iedereen moet")
# De opening: de Rol met meetbare logs (publieke Rolkaart). Zijn logs zijn aanknopingspunten voor de andere Rollen.
OPENER = "engineer"

LEEG_LOGBOEK = {
    "slots": {s: {"status": "onbekend", "bewering": None, "bewijs": [], "rollen": []} for s in SLOTS},
    "beweringen": [],
    "verwijzingen": [],
    "dwaalsporen": [],
}

PROMPTS = Path(__file__).parent / "prompts"

# >>> Kies hier welke prompt de Koning gebruikt: de naam van een bestand in prompts/ (zonder .py). <<<
PROMPT = "prompt4"


def laad_prompt():
    """Haal de prompts van de Koning uit prompts/<PROMPT>.py. Wordt bij elke oproep opnieuw ingelezen, zodat je de
    prompt tijdens een run kan bijsturen."""
    pad = PROMPTS / f"{PROMPT}.py"
    if not pad.is_file():
        beschikbaar = ", ".join(sorted(p.stem for p in PROMPTS.glob("*.py")))
        raise FileNotFoundError(f"PROMPT = {PROMPT!r} bestaat niet in prompts/; kies uit: {beschikbaar}")
    spec = importlib.util.spec_from_file_location(f"koning_{pad.stem}", pad)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return pad.stem, module.INSTRUCTIE, module.VERDICT_INSTRUCTIE

# ID's met een gewoon of een speciaal koppelteken (modellen schrijven soms U+2011 en dergelijke).
ID_PATROON = re.compile(r"\b(ARC|ENG|GUA|HIS|ORA|BEA)[-‐‑‒–—―−]([0-9]{3})\b")


def _normaliseer(waarde):
    """Vervang speciale koppeltekens in ID's door een gewoon '-', overal in een JSON-structuur."""
    if isinstance(waarde, str):
        return ID_PATROON.sub(r"\1-\2", waarde)
    if isinstance(waarde, list):
        return [_normaliseer(v) for v in waarde]
    if isinstance(waarde, dict):
        return {k: _normaliseer(v) for k, v in waarde.items()}
    return waarde


def genoemde_ids(beurten, rollen):
    """Alle ID's die echt in een bericht van een Raadslid staan: alleen die mogen in het Verdict."""
    ids = set()
    for b in beurten:
        if b["soort"] == "bericht" and b["spreker"] in rollen:
            ids.update(f"{p}-{n}" for p, n in ID_PATROON.findall(b.get("tekst", "")))
    return ids


def _filter_bewijs(lijst, toegestaan):
    """Houd alleen geldige, in het debat genoemde ID's over, zonder dubbels."""
    uit = []
    for i in lijst if isinstance(lijst, list) else []:
        if isinstance(i, str) and i in toegestaan and i not in uit:
            uit.append(i)
    return uit


def _dict(waarde):
    """Modellen geven soms een tekst of lijst waar een object hoort: behandel dat als leeg."""
    return waarde if isinstance(waarde, dict) else {}


def _tekst(waarde):
    return waarde.strip() if isinstance(waarde, str) and waarde.strip() else None


def _schoon_verdict(ruw, run_id, toegestaan, rollen):
    """Vangrails rond het Verdict van het LLM: alleen genoemde ID's, en geen bewering of uitkomst zonder bewijs."""
    oorzaak = {}
    for s in SLOTS:
        slot = _dict(_dict(ruw.get("oorzaak")).get(s))
        bewijs = _filter_bewijs(slot.get("bewijs"), toegestaan)
        bewering = _tekst(slot.get("bewering")) if bewijs else None  # schema: een bewering vraagt minstens één ID
        z = slot.get("zekerheid")
        zekerheid = min(1.0, max(0.0, float(z))) if isinstance(z, (int, float)) and not isinstance(z, bool) else None
        oorzaak[s] = {"bewering": bewering, "bewijs": bewijs if bewering else []}
        oorzaak[s]["zekerheid"] = 0.0 if bewering is None else (zekerheid if zekerheid is not None else 0.5)

    acties = []
    for a in ruw.get("acties") or []:
        if isinstance(a, dict) and _tekst(a.get("actie")) and _tekst(a.get("reden")):
            actie = {"actie": _tekst(a["actie"]), "reden": _tekst(a["reden"])}
            bewijs = _filter_bewijs(a.get("bewijs"), toegestaan)
            if bewijs:
                actie["bewijs"] = bewijs
            acties.append(actie)

    risicos = [{"risico": _tekst(r["risico"]), "mitigatie": _tekst(r["mitigatie"])}
               for r in ruw.get("risicos") or []
               if isinstance(r, dict) and _tekst(r.get("risico")) and _tekst(r.get("mitigatie"))]

    verdict = {"run_id": run_id, "samenvatting": _tekst(ruw.get("samenvatting")) or "", "oorzaak": oorzaak,
               "acties": acties, "risicos": risicos}

    v = ruw.get("voorspelling")
    if isinstance(v, dict):
        bewijs = _filter_bewijs(v.get("bewijs"), toegestaan)
        dag = v.get("dag")
        if bewijs and _tekst(v.get("bewering")) and isinstance(dag, int) and not isinstance(dag, bool) and dag >= 1:
            verdict["voorspelling"] = {"dag": dag, "bewering": _tekst(v["bewering"]), "bewijs": bewijs}

    geschillen = []
    for g in ruw.get("geschillen") or []:
        if not (isinstance(g, dict) and _tekst(g.get("onderwerp"))):
            continue
        standpunten = {r: _tekst(t) for r, t in _dict(g.get("standpunten")).items() if r in rollen and _tekst(t)}
        if not standpunten:
            continue
        bewijs = _filter_bewijs(g.get("bewijs"), toegestaan)
        uitkomst = _tekst(g.get("uitkomst")) if bewijs else None  # schema: een uitkomst vraagt minstens één ID
        geschillen.append({"onderwerp": _tekst(g["onderwerp"]), "standpunten": standpunten,
                           "uitkomst": uitkomst, "bewijs": bewijs})

    # Extra veld (het schema laat extra velden toe): wat de Koning als dwaalspoor ziet. Alleen met in het debat
    # genoemde ID's en een reden: een Rol beschuldigen kan niet zonder bewijs.
    dwaalsporen = []
    for d in ruw.get("dwaalsporen") or []:
        if not (isinstance(d, dict) and _tekst(d.get("dwaalspoor")) and _tekst(d.get("waarom"))):
            continue
        bewijs = _filter_bewijs(d.get("bewijs"), toegestaan)
        if not bewijs:
            continue
        soort = d.get("soort") if d.get("soort") in DWAALSPOOR_SOORTEN else "ander"
        betrokken = [r for r in d.get("rollen") or [] if r in rollen] if isinstance(d.get("rollen"), list) else []
        dwaalsporen.append({"dwaalspoor": _tekst(d["dwaalspoor"]), "soort": soort, "rollen": betrokken,
                            "bewijs": bewijs, "waarom": _tekst(d["waarom"])})
    verdict["dwaalsporen"] = dwaalsporen

    afweging = ruw.get("afweging") if isinstance(ruw.get("afweging"), dict) else {}
    return verdict, geschillen, afweging


SCHEMA = Path(__file__).parent / "schema"


def _structuurfouten(ruw):
    """Het LLM moet de drie slots apart invullen; anders gaat de vangrail ze stil op null zetten."""
    oorzaak = _dict(ruw.get("oorzaak"))
    mist = [s for s in SLOTS if not isinstance(oorzaak.get(s), dict)]
    if mist:
        return [f"oorzaak mist de slots {', '.join(mist)}: geef ingreep, mechanisme en verloop elk als apart object "
                '{"bewering", "bewijs", "zekerheid"}']
    return []


def _schemafouten(verdict, geschillen):
    """Toets het Verdict en de geschillen aan de schema's uit schema/."""
    v_schema = json.loads((SCHEMA / "verdict.json").read_text(encoding="utf-8"))
    t_schema = json.loads((SCHEMA / "trace.json").read_text(encoding="utf-8"))
    g_schema = {"$defs": t_schema["$defs"], "type": "array", "items": {"$ref": "#/$defs/geschil"}}
    fouten = [f"verdict {'/'.join(map(str, e.absolute_path)) or '(root)'}: {e.message}"
              for e in Draft202012Validator(v_schema).iter_errors(verdict)]
    fouten += [f"geschillen {'/'.join(map(str, e.absolute_path))}: {e.message}"
               for e in Draft202012Validator(g_schema).iter_errors(geschillen)]
    return fouten


def _slottekst(verdict, afweging):
    """Leesbaar slotbericht van de Koning voor de trace en de viewer."""
    regels = [verdict["samenvatting"], ""]
    for s in SLOTS:
        slot = verdict["oorzaak"][s]
        bewering = slot["bewering"] or "onbekend"
        regels.append(f"**{s}** ({slot.get('zekerheid', 0):.1f}): {bewering} [{', '.join(slot['bewijs'])}]")
        if afweging.get(s):
            regels.append(f"  afweging: {afweging[s]}")
    if verdict.get("dwaalsporen"):
        regels += ["", "**Dwaalsporen**"]
        for d in verdict["dwaalsporen"]:
            wie = f" ({', '.join(d['rollen'])})" if d["rollen"] else ""
            regels.append(f"- {d['soort']}{wie}: {d['dwaalspoor']} [{', '.join(d['bewijs'])}] — {d['waarom']}")
    return "\n".join(regels)


def _profielen(leden):
    regels = []
    for l in leden:
        regels.append(
            f"- {l.ROL} ({l.NAAM}): perspectief: {l.PERSPECTIEF} | Kennisbron: {getattr(l, 'KENNISBRON', '?')} | "
            f"publiek Belang: {getattr(l, 'BELANG', '?')} | "
            f"neiging: {getattr(l, 'NEIGING', '?')} | blinde vlek: {getattr(l, 'BLINDE_VLEK', '?')}"
        )
    return "\n".join(regels)


def _debat(beurten):
    """Het debat zoals de Koning het ziet: zijn eigen vragen en de berichten, met het aantal toolcalls per beurt."""
    regels, toolcalls = [], 0
    for b in beurten:
        if b["soort"] == "routering" and b.get("aan"):
            regels.append(f"[Koning → {b['aan']}] {b.get('vraag')}")
            toolcalls = 0
        elif b["soort"] == "toolcall":
            toolcalls += 1
        elif b["soort"] == "bericht" and b["spreker"] not in ("koning", "mens"):
            regels.append(f"[{b['spreker']}, {toolcalls} toolcalls] {b.get('tekst', '')}")
    return "\n\n".join(regels) or "(nog niemand sprak)"


def _lees_json(tekst):
    """Modellen zetten soms tekst of ```json rond hun antwoord: neem het buitenste {...}."""
    tekst = re.sub(r"```(?:json)?", "", tekst or "")
    begin, einde = tekst.find("{"), tekst.rfind("}")
    if begin < 0 or einde <= begin:
        raise ValueError("geen JSON-object in het antwoord")
    return json.loads(tekst[begin:einde + 1])


def _onbevestigde_slots(logboek):
    """Slots die niet 'bevestigd' zijn door minstens twee verschillende Rollen met bewijs."""
    slots = _dict(_dict(logboek).get("slots"))
    zwak = []
    for s in SLOTS:
        slot = _dict(slots.get(s))
        rollen = slot.get("rollen") if isinstance(slot.get("rollen"), list) else []
        if slot.get("status") != "bevestigd" or len(set(map(str, rollen))) < 2 or not slot.get("bewijs"):
            zwak.append(s)
    return zwak


def _rollenlijst(waarde):
    return {str(r) for r in waarde} if isinstance(waarde, list) else set()


def _toets_logboek(logboek):
    """Vangrail: 'bevestigd' vraagt een tweede, onafhankelijke Rol, net als in het Verdict.
    Een bewering of slot van één Rol, hoe precies ook gemeten, zakt terug naar 'bewering'."""
    logboek = copy.deepcopy(logboek)
    for slot in _dict(logboek.get("slots")).values():
        if isinstance(slot, dict) and slot.get("status") == "bevestigd":
            if len(_rollenlijst(slot.get("rollen"))) < 2 or not slot.get("bewijs"):
                slot["status"] = "bewering"
    for b in logboek.get("beweringen") if isinstance(logboek.get("beweringen"), list) else []:
        if isinstance(b, dict) and b.get("status") == "bevestigd":
            if not (_rollenlijst(b.get("bevestigd_door")) - {str(b.get("bron"))}):
                b["status"] = "bewering"
    return logboek


def _controleer(keuze, rollen, gesproken, stil=()):
    """Vangrails in code rond de keuze van het LLM. Geeft een foutmelding terug, of None als alles klopt.
    stil = de Rollen die nog niet spraken: zolang die er zijn, loopt de eerste fase (iedereen één keer)."""
    if not isinstance(keuze, dict):
        return "het antwoord is geen JSON-object"
    rol = keuze.get("rol")
    if rol is not None and rol not in rollen:
        return f"rol {rol!r} bestaat niet; kies uit {', '.join(rollen)} of null"
    if rol is None and gesproken == 0:
        return "je kan niet stoppen voor er iemand sprak"
    if gesproken == 0 and OPENER in rollen and rol != OPENER:
        return (f"de eerste beurt is voor de {OPENER}: zijn meetbare logs geven de aanknopingspunten "
                "voor de vragen aan de andere Rollen")
    if stil and rol is None:
        return f"je kan nog niet stoppen: eerst spreekt elk Raadslid één keer (nog niet: {', '.join(stil)})"
    if stil and rol not in stil:
        return (f"eerste fase: kies een Raadslid dat nog niet sprak ({', '.join(stil)}); "
                "diepere vragen aan wie al sprak komen daarna")
    if rol is None:
        zwak = _onbevestigde_slots(keuze.get("logboek"))
        if zwak:
            return ("je kan nog niet stoppen: de slots " + ", ".join(zwak) + " zijn niet bevestigd door minstens twee "
                    "verschillende Rollen met ID's; kies een Rol die dat kan toetsen")
    if rol is not None and not (isinstance(keuze.get("vraag"), str) and keuze["vraag"].strip()):
        return "een keuze zonder vraag"
    if rol is not None and len(keuze["vraag"].split()) > 70:
        return f"de vraag is te lang ({len(keuze['vraag'].split())} woorden): één hoofddoel, hoogstens ongeveer 50 woorden"
    reden = keuze.get("reden")
    if not (isinstance(reden, str) and reden.strip()):
        return "een keuze zonder reden"
    laag = reden.lower()
    if rol is not None and not any(d in laag for d in REDEN_DOELEN):
        return "de reden noemt geen slot (ingreep, mechanisme, verloop) of open vraag"
    if any(v in laag for v in VAGE_REDENEN):
        return "de reden is vaag; maak ze concreet (doel, waarom deze Rol, verwacht)"
    return None


class Koning:
    def __init__(self, leden):
        self.leden = leden
        self.rollen = [l.ROL for l in leden]
        self.logboek = copy.deepcopy(LEEG_LOGBOEK)
        self.open_vragen = []
        self.prompt_naam = None  # welk prompts/prompt<n>.py de laatste keuze stuurde (komt in de trace)
        self.model = os.environ["MODEL_GROOT"]
        self.opties = {"reasoning_effort": os.environ["REASONING"]} if os.getenv("REASONING") else {}
        self.client = OpenAI(base_url=os.environ["OPENAI_BASE_URL"], api_key=os.environ["OPENAI_API_KEY"], max_retries=8)
        self._verbruik = []
        self.json_modus = True  # valt terug op False als het endpoint response_format niet kent

    def neem_verbruik(self):
        """Verbruik sinds de vorige oproep, in het formaat van tel() in council.py."""
        v, self._verbruik = self._verbruik, []
        return v

    def _vraag_llm(self, berichten):
        # JSON-modus: het model moet een geldig JSON-object teruggeven. Kent het endpoint of model dat niet
        # (400 Bad Request), dan zonder JSON-modus verder voor de rest van de run.
        if self.json_modus:
            try:
                r = self.client.chat.completions.create(model=self.model, messages=berichten,
                                                        response_format={"type": "json_object"}, **self.opties)
            except BadRequestError:
                self.json_modus = False
        if not self.json_modus:
            r = self.client.chat.completions.create(model=self.model, messages=berichten, **self.opties)
        if r.usage:
            details = getattr(r.usage, "prompt_tokens_details", None)
            self._verbruik.append({
                "input_token_count": r.usage.prompt_tokens,
                "output_token_count": r.usage.completion_tokens,
                "cache_read_input_token_count": getattr(details, "cached_tokens", 0) or 0,
            })
        return r.choices[0].message.content or ""

    def kies(self, beurten, over):
        """Geeft (rol, vraag, reden, open_vragen) terug; rol None = het debat stopt."""
        gesproken = sum(1 for b in beurten if b["soort"] == "bericht" and b["spreker"] in self.rollen)
        sprekers = {b["spreker"] for b in beurten if b["soort"] == "bericht"}
        stil = [r for r in self.rollen if r not in sprekers]
        self.prompt_naam, instructie, _ = laad_prompt()  # telkens opnieuw: aanpassingen gelden vanaf deze keuze
        fase = (f"FASE 1: nog niet gesproken: {', '.join(stil)}." if stil else
                "FASE 2: iedereen sprak; leg links tussen de antwoorden en stel diepere vragen.")
        berichten = [
            {"role": "system", "content": instructie},
            {"role": "user", "content": (
                f"RAADSLEDEN (publieke Rolkaarten)\n{_profielen(self.leden)}\n\n"
                f"BEURTENBUDGET: {gesproken} gebruikt, nog {over} over. {fase}\n\n"
                f"JE LOGBOEK TOT NU TOE\n{json.dumps(self.logboek, ensure_ascii=False)}\n\n"
                f"DEBAT TOT NU TOE\n{_debat(beurten)}\n\n"
                "Werk je logboek bij en kies de volgende stap."
            )},
        ]
        fout = None
        for _ in range(3):  # twee nieuwe pogingen als de keuze niet door de vangrails komt, daarna de fallback
            try:
                antwoord = self._vraag_llm(berichten)
                keuze = _lees_json(antwoord)
                fout = _controleer(keuze, self.rollen, gesproken, stil)
            except Exception as e:  # netwerk- of JSON-fout: tweede poging, daarna fallback
                antwoord, keuze, fout = "", None, f"{type(e).__name__}: {e}"
            if fout is None:
                if isinstance(keuze.get("logboek"), dict):
                    self.logboek = _toets_logboek(keuze["logboek"])
                open_vragen = keuze.get("open_vragen")
                self.open_vragen = [str(v) for v in open_vragen] if isinstance(open_vragen, list) else self.open_vragen
                return keuze.get("rol"), keuze.get("vraag"), keuze["reden"].strip(), self.open_vragen
            berichten += [
                {"role": "assistant", "content": antwoord or "(geen antwoord)"},
                {"role": "user", "content": f"Je keuze is ongeldig: {fout}. Antwoord opnieuw met alleen het JSON-object."},
            ]
        return self._fallback(beurten, fout)

    def verdict(self, beurten, run_id):
        """Stel het Verdict op uit het debat. Geeft (verdict, geschillen, afweging, slottekst) terug."""
        toegestaan = genoemde_ids(beurten, self.rollen)
        self.prompt_naam, _, verdict_instructie = laad_prompt()
        berichten = [
            {"role": "system", "content": verdict_instructie},
            {"role": "user", "content": (
                f"RAADSLEDEN (publieke Rolkaarten)\n{_profielen(self.leden)}\n\n"
                f"JE LOGBOEK\n{json.dumps(self.logboek, ensure_ascii=False)}\n\n"
                f"DEBAT\n{_debat(beurten)}\n\n"
                "Stel de afweging, het Verdict, de geschillen en de dwaalsporen op."
            )},
        ]
        fout = None
        for _ in range(3):  # twee nieuwe pogingen met de foutmeldingen erbij
            try:
                antwoord = self._vraag_llm(berichten)
                ruw = _dict(_normaliseer(_lees_json(antwoord)))
                verdict, geschillen, afweging = _schoon_verdict(ruw, run_id, toegestaan, self.rollen)
                fouten = _structuurfouten(ruw) + _schemafouten(verdict, geschillen)
                fout = "; ".join(fouten) if fouten else None
            except Exception as e:
                antwoord, fout = "", f"{type(e).__name__}: {e}"
            if fout is None:
                return verdict, geschillen, afweging, _slottekst(verdict, afweging)
            berichten += [
                {"role": "assistant", "content": antwoord or "(geen antwoord)"},
                {"role": "user", "content": f"Je Verdict is ongeldig: {fout}. Antwoord opnieuw met alleen het JSON-object."},
            ]
        verdict = {
            "run_id": run_id,
            "samenvatting": f"De Koning kon geen geldig Verdict opstellen ({fout}). De oorzaak blijft onbekend.",
            "oorzaak": {s: {"bewering": None, "bewijs": [], "zekerheid": 0.0} for s in SLOTS},
            "acties": [{"actie": "Het debat opnieuw voeren", "reden": "Dit debat leverde geen geldig Verdict op."}],
            "risicos": [{"risico": "Zonder gekende oorzaak kan de Desync terugkeren.",
                         "mitigatie": "Een nieuw debat met gerichte vragen per slot."}],
        }
        return verdict, [], {}, verdict["samenvatting"]

    def _fallback(self, beurten, fout):
        """Geen geldige keuze na drie pogingen: een eerlijke keuze in code, met de reden erbij."""
        aantal = {r: 0 for r in self.rollen}
        for b in beurten:
            if b["soort"] == "bericht" and b["spreker"] in aantal:
                aantal[b["spreker"]] += 1
        if not any(aantal.values()) and OPENER in self.rollen:
            vraag = ("Welke meetbare afwijkingen tonen je logs rond de Desync? Noem per afwijking tijdstip, plaats "
                     "en record-ID's, en naar welke Rol je bron verwijst.")
            reden = (f"Fallback: de Koning gaf geen geldige keuze ({fout}). Doel: de slots ingreep en verloop "
                     f"openen. Waarom de {OPENER}: zijn meetbare logs geven tijdstip en plaats als aanknopingspunt. "
                     "Verwacht: ID's waarmee de Koning daarna andere Rollen gericht kan vragen wat er buiten het "
                     "datanet gebeurde.")
            return OPENER, vraag, reden, self.open_vragen
        stil = [r for r in self.rollen if aantal[r] == 0]
        if stil:  # eerste fase: iedereen één keer
            rol = stil[0]
            open_slots = [s for s in SLOTS
                          if _dict(_dict(self.logboek.get("slots")).get(s)).get("status") in (None, "onbekend")]
            doel = ", ".join(open_slots) or "ingreep, mechanisme en verloop"
            vraag = f"Wat zegt je bron over {doel}? Noem record-ID's, en naar welke Rol je bron verwijst."
            reden = (f"Fallback: de Koning gaf geen geldige keuze ({fout}). Doel: open slots {doel}. "
                     f"De {rol} sprak nog niet (eerste fase: iedereen één keer).")
            return rol, vraag, reden, self.open_vragen
        # Tweede fase: het zwakste slot (eerst onbekend, dan onbevestigd) bij de Rol die het langst niet sprak.
        slots = _dict(self.logboek.get("slots"))
        zwak = _onbevestigde_slots(self.logboek) or list(SLOTS)
        slot = min(zwak, key=lambda s: _dict(slots.get(s)).get("status") not in (None, "onbekend"))
        bewering = _tekst(_dict(slots.get(slot)).get("bewering"))
        laatst = {r: -1 for r in self.rollen}
        for i, b in enumerate(beurten):
            if b["soort"] == "bericht" and b["spreker"] in laatst:
                laatst[b["spreker"]] = i
        rol = min(self.rollen, key=lambda r: laatst[r])
        if bewering:
            vraag = (f"Over het slot {slot} luidt de bewering: \"{bewering[:200]}\". Bevestigt of weerlegt jouw bron "
                     "dat, met record-ID's?")
        else:
            vraag = f"Wat toont jouw bron over het slot {slot}: wie deed wat, of hoe werkte het? Noem record-ID's."
        reden = (f"Fallback: de Koning gaf geen geldige keuze ({fout}). Doel: het slot {slot}, dat nog niet bevestigd "
                 f"is door twee Rollen. Waarom de {rol}: die sprak het langst niet en kan het als tweede bron toetsen.")
        return rol, vraag, reden, self.open_vragen
