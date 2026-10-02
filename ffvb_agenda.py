#!/usr/bin/env python3
"""
Matchs FFVB de PAM 2 (ffvbbeach.org) -> fichier .ics à importer à la main dans Google Agenda.

Usage :
  python ffvb_agenda.py

À chaque lancement : lit le calendrier, retélécharge TOUTES les feuilles de match dans
./feuilles (les anciennes sont écrasées, même sans changement : salle ou horaire ont pu
bouger) et réécrit agenda/pam2.ics. Si une feuille manque ou est illisible, le .ics
n'est pas réécrit (le précédent est conservé).

Dépendances : pip install -r requirements.txt
"""
import glob
import hashlib
import os
import re
import sys
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import pdfplumber
import requests
from bs4 import BeautifulSoup

# --- Configuration -----------------------------------------------------------
URL = "https://www.ffvbbeach.org/ffvbapp/resu/vbspo_calendrier.php"
PARAMS = {
    "saison": "2026/2027",
    "codent": "PTLO54",
    "poule": "OP3",
    "calend": "COMPLET",
    "equipe": "2",
}
MON_EQUIPE = "PONT-A-MOUSSON 2"
ALIAS = "PAM 2"
DUREE = timedelta(hours=2)
TZ = ZoneInfo("Europe/Paris")
DOSSIER_FDM = "feuilles"
FICHIER_ICS = os.path.join("agenda", "pam2.ics")
JOURS = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]

# --- Calendrier --------------------------------------------------------------
RE_CODE = re.compile(r"^[A-Z0-9]{2,4}[AR]\d{3}$")       # ex. OP3A002 / OP3R047
RE_DATE = re.compile(r"^\d{2}/\d{2}/\d{2}$")
RE_HEURE = re.compile(r"^\d{1,2}:\d{2}$")
RE_SCORE = re.compile(r"^\d+\s*[-/]\s*\d+$")             # score en sets "3/1"
RE_SETS = re.compile(r"\d+\s*:\s*\d+")                   # détail des sets "25:20 ..."
RE_URL_JS = re.compile(r"""['"]([^'"\s]+?\.(?:php|pdf)[^'"\s]*)['"]""", re.I)
INDICES_FDM = ("pdf", "feuille", "fdm")


@dataclass
class Match:
    code: str
    debut: datetime
    dom: str
    ext: str
    score: str | None
    fdm_req: tuple | None         # (méthode, url, données) à rejouer pour obtenir le PDF
    fdm_fichier: str | None = None
    # Lu dans la feuille de match
    salle: str | None = None
    adresse: str | None = None
    heure_feuille: str | None = None   # "20h30" si elle diffère de l'heure du calendrier
    club_adv: dict | None = None       # cf. _clubs
    club_nous: dict | None = None

    @property
    def domicile(self) -> bool:
        return self.dom == MON_EQUIPE

    @property
    def adversaire(self) -> str:
        return self.ext if self.domicile else self.dom

    @property
    def lieu(self) -> str:
        return ", ".join(x for x in (self.salle, self.adresse) if x)

    @property
    def uid(self) -> str:
        # Stable d'un export à l'autre : un réimport remplace l'événement au lieu de le dupliquer.
        cle = f"{PARAMS['saison']}|{PARAMS['codent']}|{self.code}"
        return "ffvb" + hashlib.sha1(cle.encode()).hexdigest()[:28] + "@ffvb"


def nouvelle_session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                               "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
    return s


def recuperer_page(session: requests.Session) -> tuple[str, str]:
    r = session.get(URL, params=PARAMS, timeout=30)
    r.raise_for_status()
    # Latin-1/CP1252 pas toujours déclaré -> "CL�MENCEAU" sinon.
    r.encoding = "cp1252"
    return r.text, r.url


def _attrs_texte(el) -> str:
    return " ".join(v if isinstance(v, str) else " ".join(v) for v in el.attrs.values()).lower()


def _url_dans(el) -> str | None:
    """URL portée par un élément : href/action directs, ou URL dans du JS (onclick, href=javascript:)."""
    for attr in ("href", "action", "data-href", "data-url", "onclick"):
        v = (el.get(attr) or "").strip()
        if not v or v == "#":
            continue
        if attr != "onclick" and not v.lower().startswith("javascript"):
            return v
        m = RE_URL_JS.search(v)
        if m:
            return m.group(1)
    return None


def requete_formulaire(form, base: str, bouton=None) -> tuple[str, str, list]:
    """(méthode, url, données) équivalents à la soumission du formulaire par un navigateur."""
    url = urljoin(base, form.get("action") or base)
    methode = (form.get("method") or "get").lower()
    data = []
    for el in form.find_all(["input", "select", "textarea"]):
        nom = el.get("name")
        if not nom or el.has_attr("disabled"):
            continue
        t = (el.get("type") or "text").lower()
        if el.name == "select":
            opt = el.find("option", selected=True) or el.find("option")
            if opt is not None:
                data.append((nom, opt.get("value", opt.get_text())))
        elif el.name == "textarea":
            data.append((nom, el.get_text()))
        elif t in ("checkbox", "radio"):
            if el.has_attr("checked"):
                data.append((nom, el.get("value", "on")))
        elif t in ("submit", "image", "button", "reset", "file"):
            continue  # seul le bouton effectivement cliqué est envoyé
        else:
            data.append((nom, el.get("value", "")))
    # Un <input type="image" name="x"> cliqué envoie les coordonnées x.x / x.y
    if bouton is not None and bouton.get("name"):
        n = bouton["name"]
        if (bouton.get("type") or "").lower() == "image":
            data += [(f"{n}.x", "7"), (f"{n}.y", "7")]
        else:
            data.append((n, bouton.get("value", "")))
    return methode, url, data


def requete_fdm(tr, base: str) -> tuple | None:
    """Repère l'icône PDF de la ligne et reconstruit la requête qu'elle déclenche."""
    for el in tr.find_all(True):
        if not any(k in _attrs_texte(el) for k in INDICES_FDM):
            continue
        # 1) URL explicite (href, window.open dans un onclick...)
        for cand in (el, el.find_parent("a")):
            if cand is not None and (url := _url_dans(cand)):
                return "get", urljoin(base, url), []
        # 2) <input type="image"> / bouton : soumission du formulaire englobant
        if el.name in ("input", "button"):
            form = el.find_parent("form")
            if form is None and el.get("form"):          # attribut HTML5 form="id"
                form = list(el.parents)[-1].find("form", id=el["form"])
            if form is not None:
                return requete_formulaire(form, base, el)
    return None


def parser(html: str, base: str) -> list[Match]:
    soup = BeautifulSoup(html, "html.parser")
    matchs = []
    for tr in soup.find_all("tr"):
        # recursive=False : ignore les <tr> de mise en page qui englobent tout le tableau
        cells = [td.get_text(" ", strip=True) for td in tr.find_all("td", recursive=False)]
        i_code = next((i for i, c in enumerate(cells) if RE_CODE.match(c)), None)
        if i_code is None:
            continue

        reste = cells[i_code + 1:]
        date = next((c for c in reste if RE_DATE.match(c)), None)
        heure = next((c for c in reste if RE_HEURE.match(c)), None)
        if not date:
            continue
        apres = reste[reste.index(heure if heure else date) + 1:]

        textes = [c for c in apres
                  if c and not c.isdigit() and not RE_SCORE.match(c) and not RE_SETS.search(c)]
        if len(textes) < 2:
            continue
        dom, ext = textes[0], textes[1]
        if MON_EQUIPE not in (dom, ext):
            continue

        matchs.append(Match(
            code=cells[i_code],
            debut=datetime.strptime(f"{date} {heure or '20:00'}", "%d/%m/%y %H:%M").replace(tzinfo=TZ),
            dom=dom,
            ext=ext,
            score=next((c for c in apres if RE_SCORE.match(c)), None),
            fdm_req=requete_fdm(tr, base),
        ))
    return sorted(matchs, key=lambda m: m.debut)


# --- Feuilles de match -------------------------------------------------------
RE_REFRESH = re.compile(r"url\s*=\s*['\"]?([^'\";]+)", re.I)


def _slug(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()


def recuperer_pdf(session: requests.Session, req: tuple, referer: str, sauts: int = 3) -> bytes | None:
    """Rejoue la requête ; si on reçoit une page intermédiaire (formulaire auto-soumis,
    iframe/embed, meta refresh), on la suit comme le ferait le navigateur."""
    methode, url, data = req
    for _ in range(sauts + 1):
        hdr = {"Referer": referer}
        if methode == "post":
            r = session.post(url, data=data, headers=hdr, timeout=60)
        else:
            r = session.get(url, params=data or None, headers=hdr, timeout=60)
        r.raise_for_status()
        if r.content.startswith(b"%PDF"):
            return r.content
        r.encoding = "cp1252"
        soup = BeautifulSoup(r.text, "html.parser")
        referer = r.url
        cible = soup.find(["iframe", "embed", "frame"], src=True) or soup.find("object", data=True)
        meta = soup.find("meta", attrs={"http-equiv": re.compile("refresh", re.I)})
        form = soup.find("form")
        if cible is not None:
            methode, url, data = "get", urljoin(r.url, cible.get("src") or cible.get("data")), []
        elif meta is not None and (m := RE_REFRESH.search(meta.get("content", ""))):
            methode, url, data = "get", urljoin(r.url, m.group(1).strip()), []
        elif form is not None:
            bouton = (form.find("input", type=re.compile("^(submit|image)$", re.I))
                      or form.find("button"))
            methode, url, data = requete_formulaire(form, r.url, bouton)
        else:
            break
    return None


def telecharger_fdm(session: requests.Session, matchs: list[Match], referer: str) -> list[str]:
    """Vide ./feuilles puis retélécharge toutes les feuilles. Renvoie les erreurs."""
    os.makedirs(DOSSIER_FDM, exist_ok=True)
    for ancien in glob.glob(os.path.join(DOSSIER_FDM, "*.pdf")):
        os.remove(ancien)
    erreurs = []
    for m in matchs:
        if not m.fdm_req:
            erreurs.append(f"{m.code} : icône de feuille de match introuvable sur le calendrier")
            continue
        try:
            pdf = recuperer_pdf(session, m.fdm_req, referer)
        except requests.RequestException as e:
            erreurs.append(f"{m.code} : téléchargement de la feuille impossible ({e})")
            continue
        if pdf is None:
            erreurs.append(f"{m.code} : le site n'a pas renvoyé de PDF")
            continue
        m.fdm_fichier = os.path.join(DOSSIER_FDM, f"{m.debut:%Y-%m-%d}_{m.code}_{_slug(m.adversaire)}.pdf")
        with open(m.fdm_fichier, "wb") as f:
            f.write(pdf)
    return erreurs


# --- Analyse des feuilles de match -------------------------------------------
# Structure de la fiche FFVB :
#   - "Match: OP3A002 - Lundi 05 Octobre 2026 à 20h00"
#   - bloc "Salle" : salle du match + adresse, jusqu'à "Arbitre.s"
#   - un bloc par club (recevant puis visiteur), en 3 colonnes :
#     Correspondant | Salle principale | Salle de repli
#   En texte brut, les colonnes sont fusionnées ligne à ligne
#   ("M. JEAN DUPONT PARC DES SPORTS"), d'où l'extraction par position.
RE_MAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
RE_TEL = re.compile(r"(?:\+33\s?|0)[1-9](?:[\s.\-]?\d{2}){4}")
RE_ENTETE_CLUB = re.compile(r"\d+\s*-\s*\d{6,8}\s+(.+?)\s+Ligue\b")
RE_CIVILITE = re.compile(r"^(M\.|MR\.?|MME\.?|MLLE\.?)\s+", re.I)
RE_LIBELLE = re.compile(r"^\S{1,8}\s*:")       # "Port.:", "M?l.:" (encodage cassé dans la fiche)
RE_EQ = re.compile(r"\bEQ\s*(\d+)\s*:\s*", re.I)  # un correspondant par équipe : "EQ1: NOM", "Tél.: EQ1: 06..."
RE_NUM_EQUIPE = re.compile(r"\s(\d+)$")          # "LUNEVILLE 2" -> 2
RE_HEURE_FEUILLE = re.compile(r"^Match\s*:.*?(\d{1,2})h(\d{2})")


def _norm(s: str) -> str:
    return " ".join(unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().upper().split())


def _fmt_tel(t: str) -> str:
    chiffres = re.sub(r"\D", "", t)
    if chiffres.startswith("33"):
        chiffres = "0" + chiffres[2:]
    return " ".join(chiffres[i:i + 2] for i in range(0, 10, 2))


def _salle_match(lignes: list[str]) -> tuple[str | None, str | None]:
    lignes = [" ".join(l.split()) for l in lignes]
    if "Salle" not in lignes:
        return None, None
    bloc = []
    for l in lignes[lignes.index("Salle") + 1:]:
        if not l or l.lower().startswith("arbitre"):
            break
        bloc.append(l)
    if not bloc:
        return None, None
    return bloc[0], ", ".join(bloc[1:]) or None


def _clubs(page) -> list[dict]:
    """Blocs club dans l'ordre de la fiche, colonnes lues par position.
    -> [{"club", "contacts", "salle_repli"}]"""
    entetes = page.search(r"\d+ - \d{6,8} .+? Ligue")
    res = []
    for c in page.search(r"Correspondant\s*:"):
        def x_libelle(motif, defaut):
            return next((s["x0"] for s in page.search(motif) if abs(s["top"] - c["top"]) < 3), defaut)
        x_princ = x_libelle(r"Salle principale", page.width / 3)
        x_repli = x_libelle(r"Salle de repli", page.width)
        bas = [e["top"] for e in entetes if e["top"] > c["bottom"]]
        y1 = min(bas) if bas else min(c["bottom"] + 80, page.height)

        def colonne(x0, x1):
            # within_bbox (et pas crop) : ne garde que les caractères entièrement dans la zone,
            # sinon le libellé "Correspondant :" se mélange à la ligne du nom.
            txt = page.within_bbox((x0, c["bottom"], min(x1, page.width), y1)).extract_text() or ""
            return [l.strip() for l in txt.splitlines() if l.strip()]

        corr = colonne(0, x_princ - 1)
        entete = next((e for e in reversed(entetes) if e["top"] < c["top"]), None)
        club = RE_ENTETE_CLUB.search(entete["text"]) if entete else None
        res.append({
            "club": club.group(1) if club else None,
            "contacts": _contacts_bloc([RE_CIVILITE.sub("", l) for l in corr], "\n".join(corr)),
            "salle_repli": ", ".join(colonne(x_repli - 1, page.width)) or None,
        })
    return res


def _a_un_nom(s: str) -> bool:
    return len(re.findall(r"[A-Za-zÀ-ÿ]", s)) >= 4


def _contacts_bloc(lignes: list[str], txt: str) -> list[dict]:
    """Un contact, ou un par équipe quand la colonne est découpée en EQ1/EQ2... :
         EQ1: JEAN DUPONT / EQ2: MARIE MARTIN / mail1 / mail2 / ... / Tél.: EQ1: 06...
    Les mails n'ont pas d'étiquette : attribués dans l'ordre des équipes."""
    equipes = sorted({int(n) for n in RE_EQ.findall(txt)})
    if len(equipes) < 2:
        nom = next((l for l in lignes if not RE_LIBELLE.match(l) and _a_un_nom(l)), None)
        tel, mail = RE_TEL.search(txt), RE_MAIL.search(txt)
        return [{"equipe": None, "nom": nom,
                 "tel": _fmt_tel(tel.group()) if tel else None,
                 "mail": mail.group() if mail else None}]

    par_eq = {n: {"equipe": n, "nom": None, "tel": None, "mail": None} for n in equipes}
    mails_libres = []
    for l in lignes:
        m = RE_EQ.search(l)
        if m is None:
            mails_libres += [x for x in RE_MAIL.findall(l) if x not in mails_libres]
            continue
        ct, reste = par_eq[int(m.group(1))], l[m.end():].strip()
        if (tel := RE_TEL.search(reste)):
            ct["tel"] = ct["tel"] or _fmt_tel(tel.group())
        elif (mail := RE_MAIL.search(reste)):
            ct["mail"] = ct["mail"] or mail.group()
        elif _a_un_nom(reste):
            ct["nom"] = ct["nom"] or reste
    sans_mail = [ct for ct in par_eq.values() if not ct["mail"]]
    for ct, mail in zip(sans_mail, (x for x in mails_libres
                                    if x not in {c["mail"] for c in par_eq.values()})):
        ct["mail"] = mail
    return list(par_eq.values())


def _ordonner_contacts(contacts: list[dict], equipe: str) -> list[dict]:
    """EQn ne correspond pas forcément à l'équipe « CLUB n » : on garde tous les contacts,
    celui dont le numéro coïncide en tête et marqué « probable »."""
    num = RE_NUM_EQUIPE.search(equipe)
    if len(contacts) < 2 or num is None:
        return contacts
    n = int(num.group(1))
    return sorted((dict(c, probable=c["equipe"] == n) for c in contacts),
                  key=lambda c: not c["probable"])


def analyser_fdm(m: Match) -> str | None:
    """Remplit salle, adresse, heure et clubs depuis la feuille. Renvoie une erreur éventuelle."""
    try:
        with pdfplumber.open(m.fdm_fichier) as pdf:
            page = pdf.pages[0]
            lignes = [l.strip() for l in (page.extract_text() or "").splitlines()]
            clubs = _clubs(page)
    except Exception as e:
        return f"{m.code} : feuille illisible ({e})"

    m.salle, m.adresse = _salle_match(lignes)
    if not m.salle:
        return f"{m.code} : salle introuvable dans la feuille"

    h = next((RE_HEURE_FEUILLE.match(l) for l in lignes if RE_HEURE_FEUILLE.match(l)), None)
    if h and (int(h.group(1)), int(h.group(2))) != (m.debut.hour, m.debut.minute):
        m.heure_feuille = f"{int(h.group(1))}h{h.group(2)}"

    # Les blocs club suivent l'ordre recevant / visiteur de l'en-tête "EQUIPE1 ----- EQUIPE2"
    recevant = lignes[lignes.index("-----") - 1] if "-----" in lignes[1:] else m.dom
    idx_adv = 1 if _norm(recevant) == _norm(MON_EQUIPE) else 0
    if len(clubs) == 2:
        m.club_adv, m.club_nous = clubs[idx_adv], clubs[1 - idx_adv]
    return None


# --- Export .ics -------------------------------------------------------------
def titre(m: Match) -> str:
    t = f"🏐 {ALIAS} – {m.adversaire} ({'dom.' if m.domicile else 'ext.'})"
    return f"{t} [{m.score}]" if m.score else t


def fmt_contact(c: dict) -> str:
    nom = c["nom"]
    if c["equipe"] is not None:
        nom = f"EQ{c['equipe']} : {nom or '?'}" + (" (probablement cette équipe)" if c.get("probable") else "")
    return " – ".join(x for x in (nom, c["tel"], c["mail"]) if x)


def description(m: Match, page_url: str) -> str:
    """Ce qu'il faut pour le match sans ouvrir la feuille : équipes, date, gymnase, contacts."""
    d = m.debut
    lignes = [f"{m.dom} (reçoit) contre {m.ext}",
              f"{JOURS[d.weekday()]} {d:%d/%m/%Y} à {d:%Hh%M}"]
    if m.heure_feuille:
        lignes.append(f"⚠ Heure différente sur la feuille de match : {m.heure_feuille}")
    lignes.append(f"Gymnase : {m.salle}")
    if m.adresse:
        lignes.append(f"Adresse : {m.adresse}")
    recevant = m.club_nous if m.domicile else m.club_adv
    if recevant and recevant["salle_repli"]:
        lignes.append(f"Salle de repli : {recevant['salle_repli']}")

    # Contacts des deux clubs, l'adversaire d'abord
    for equipe, club in ((m.adversaire, m.club_adv), (MON_EQUIPE, m.club_nous)):
        if not club:
            continue
        lignes += ["", f"Contact {equipe}" + (f" ({club['club']})" if club["club"] else "") + " :"]
        lignes += [f"- {fmt_contact(c)}" for c in _ordonner_contacts(club["contacts"], equipe)]

    lignes += ["", f"Match {m.code} – à vérifier sur le calendrier FFVB :", page_url]
    return "\n".join(lignes)


def _ics_esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def ecrire_ics(matchs: list[Match], page_url: str) -> None:
    fmt = "%Y%m%dT%H%M%SZ"
    maintenant = datetime.now(timezone.utc)
    now = maintenant.strftime(fmt)
    # Réimport dans le même agenda : même UID = mise à jour ; une SEQUENCE croissante
    # (secondes depuis 2026) signale que cette version remplace la précédente.
    sequence = int((maintenant - datetime(2026, 1, 1, tzinfo=timezone.utc)).total_seconds())
    lignes = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//perso//ffvb-agenda//FR"]
    for m in matchs:
        lignes += [
            "BEGIN:VEVENT",
            f"UID:{m.uid}",
            f"DTSTAMP:{now}",
            f"LAST-MODIFIED:{now}",
            f"SEQUENCE:{sequence}",
            f"DTSTART:{m.debut.astimezone(timezone.utc).strftime(fmt)}",
            f"DTEND:{(m.debut + DUREE).astimezone(timezone.utc).strftime(fmt)}",
            f"SUMMARY:{_ics_esc(titre(m))}",
            f"LOCATION:{_ics_esc(m.lieu)}",
            f"DESCRIPTION:{_ics_esc(description(m, page_url))}",
            "END:VEVENT",
        ]
    lignes.append("END:VCALENDAR")
    os.makedirs(os.path.dirname(FICHIER_ICS), exist_ok=True)
    with open(FICHIER_ICS, "w", encoding="utf-8", newline="") as f:
        f.write("\r\n".join(lignes) + "\r\n")


# --- Main --------------------------------------------------------------------
def main() -> None:
    # Sortie redirigée sous Windows (cp1252) : un caractère spécial ne doit pas faire planter
    for flux in (sys.stdout, sys.stderr):
        flux.reconfigure(errors="replace")

    session = nouvelle_session()
    html, page_url = recuperer_page(session)
    matchs = parser(html, page_url)
    if not matchs:
        sys.exit("Aucun match trouvé sur le calendrier : la page a peut-être changé de structure.")

    erreurs = telecharger_fdm(session, matchs, page_url)
    erreurs += [e for m in matchs if m.fdm_fichier and (e := analyser_fdm(m))]
    if erreurs:
        sys.exit("\n".join(erreurs) + f"\n\n{FICHIER_ICS} n'a PAS été modifié (lieux ou contacts incomplets).")

    for m in matchs:
        alerte = f"  ⚠ heure feuille : {m.heure_feuille}" if m.heure_feuille else ""
        print(f"{m.debut:%d/%m/%y %H:%M}  {'DOM' if m.domicile else 'EXT'}  "
              f"{m.adversaire:<18} {m.salle}{alerte}")
    ecrire_ics(matchs, page_url)
    print(f"\n{len(matchs)} matchs écrits dans {FICHIER_ICS} – {len(matchs)} feuilles dans {DOSSIER_FDM}/")


if __name__ == "__main__":
    main()
