#!/usr/bin/env python3
"""Conversion de projets QGIS vers `pg_service.conf`.

Pour chaque couche PostgreSQL/PostGIS a connexion embarquee, remplace
host=/port=/dbname= par service='<nom>'. Tout le reste de la chaine de connexion
(user=, password=, sslmode=, table=, key=, srid=, type=, sql=...) est laisse
strictement intact.

Un seul nom de service pour tous les projets : le service designe un serveur et
ne porte aucun droit. Le role PostgreSQL reste celui du projet (user=) ou,
quand la couche n'en porte pas, celui de la connexion enregistree dans le
profil QGIS de l'agent.

Couches laissees de cote, signalees dans le journal :
- deja converties (service= present) : le script est idempotent ;
- authcfg : conversion manuelle (configuration d'authentification a part) ;
- sans host= : anomalie a examiner ;
- pointant vers un host exclu (--exclude-host).

Perimetre : soit un dossier entier (argument `racine`), soit une liste de
projets (--liste), un chemin par ligne. Le mode liste sert a convertir un panel
de test choisi projet par projet.

Plusieurs services vers des bases differentes sur un meme host : un seul nom
de service ne peut pas convenir a toutes les couches. Restreindre chaque
passage a une base avec --dbname (repetable), et faire un passage par
service. Exemple pour deux bases sur le meme serveur :
    python convert_qgis_pgservice.py L:/SIG --service srv_db_a --dbname db_a --apply
    python convert_qgis_pgservice.py L:/SIG --service srv_db_b --dbname db_b --apply
Une couche dont le dbname= n'est pas dans la liste --dbname est laissee
intacte et journalisee avec le statut "dbname_non_visee" (elle peut etre
traitee dans un autre passage).

Necessite lxml (pip install lxml) plutot que xml.etree.ElementTree, pour
preserver commentaires et instructions de traitement du XML d'origine.

Usage :
    python convert_qgis_pgservice.py <racine> [--service prod]
        [--exclude-host 203.0.113.10] [--dbname db_a]
        [--apply] [--backup-dir DOSSIER] [--log FICHIER]
    python convert_qgis_pgservice.py --liste panel.txt --backup-dir DOSSIER [--apply] ...

Le mode par defaut est une simulation : rien n'est ecrit sur le disque.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import os
import re
import sys
import zipfile
from pathlib import Path

try:
    from lxml import etree
except ImportError:
    sys.exit(
        "Ce script necessite lxml (pip install lxml). "
        "xml.etree.ElementTree ne preserve pas assez fidelement les fichiers "
        "projet QGIS d'origine pour une conversion en masse sur de la production."
    )

PG_PROVIDERS = {"postgres", "postgresraster"}
PARAMS_REMPLACES = ("host", "port", "dbname")
BACKUP_DIRNAME = "_backup_pgservice"

# Valeur entre guillemets simples (apostrophe doublee geree), ou non quotee.
TOKEN_RE = re.compile(r"(?P<ws>\s*)(?P<key>\w+)=(?P<val>'(?:[^']|'')*'|\S*)")

LOG_FIELDS = [
    "chemin_projet",
    "id_couche",
    "nom_couche",
    "statut",
    "host_avant",
    "service",
    "datasource_avant",
    "datasource_apres",
]


def _unescape(val: str) -> str:
    if len(val) >= 2 and val.startswith("'") and val.endswith("'"):
        return val[1:-1].replace("''", "'")
    if len(val) >= 2 and val.startswith('"') and val.endswith('"'):
        return val[1:-1]
    return val


def get_param(datasource: str, key: str) -> str | None:
    for m in TOKEN_RE.finditer(datasource):
        if m.group("key") == key:
            return _unescape(m.group("val")) or None
    return None


def mask_password(datasource: str) -> str:
    m = re.search(r"password='(?:[^']|'')*'", datasource)
    if m:
        return datasource[: m.start()] + "password='***'" + datasource[m.end() :]
    m = re.search(r"password=\S+", datasource)
    if m:
        return datasource[: m.start()] + "password=***" + datasource[m.end() :]
    return datasource


def rewrite_datasource(datasource: str, service: str) -> str:
    """Remplace host= par service= et supprime port=/dbname=, sans toucher au
    reste. Les remplacements sont appliques de droite a gauche pour que les
    positions calculees restent valides."""
    tokens = list(TOKEN_RE.finditer(datasource))
    host_match = next((m for m in tokens if m.group("key") == "host"), None)
    a_supprimer = [m for m in tokens if m.group("key") in ("port", "dbname")]

    edits: list[tuple[int, int, str]] = []
    if host_match is not None:
        edits.append((host_match.start("key"), host_match.end("val"), f"service='{service}'"))
    else:
        edits.append((0, 0, f"service='{service}' "))
    for m in a_supprimer:
        edits.append((m.start("ws"), m.end("val"), ""))

    edits.sort(key=lambda e: e[0], reverse=True)
    new_ds = datasource
    for start, end, repl in edits:
        new_ds = new_ds[:start] + repl + new_ds[end:]
    # Supprimer un parametre en tete laisse un espace initial, sans effet pour
    # QGIS mais genant a la relecture.
    return new_ds.strip()


class ConversionResult:
    __slots__ = ("statut", "host", "service", "avant", "apres")

    def __init__(self, statut: str, host: str | None, service: str | None, avant: str, apres: str):
        self.statut = statut
        self.host = host
        self.service = service
        self.avant = avant
        self.apres = apres


def convert_layer(
    datasource: str,
    service: str,
    exclude_hosts: set[str],
    dbnames: set[str] | None = None,
) -> ConversionResult:
    host = get_param(datasource, "host")

    if get_param(datasource, "service") is not None:
        return ConversionResult("deja_converti", host, None, datasource, datasource)
    if get_param(datasource, "authcfg") is not None:
        return ConversionResult("authcfg_manuel", host, None, datasource, datasource)
    if dbnames and get_param(datasource, "dbname") not in dbnames:
        return ConversionResult("dbname_non_visee", host, None, datasource, datasource)
    if host is None:
        return ConversionResult("sans_host", None, None, datasource, datasource)
    if host in exclude_hosts:
        return ConversionResult("host_exclu", host, None, datasource, datasource)

    try:
        new_ds = rewrite_datasource(datasource, service)
    except Exception:
        return ConversionResult("erreur_transformation", host, service, datasource, datasource)

    # Le service doit etre pose, et plus aucun parametre d'adressage ne doit
    # subsister.
    if get_param(new_ds, "service") != service:
        return ConversionResult("erreur_transformation", host, service, datasource, datasource)
    if any(get_param(new_ds, p) is not None for p in PARAMS_REMPLACES):
        return ConversionResult("erreur_transformation", host, service, datasource, datasource)

    return ConversionResult("converti", host, service, datasource, new_ds)


def process_xml(
    xml_bytes: bytes,
    service: str,
    exclude_hosts: set[str],
    chemin_log: str,
    rows: list[dict[str, str]],
    dbnames: set[str] | None = None,
):
    """Retourne (nouveaux_bytes_ou_None, nb_conversions)."""
    parser = etree.XMLParser(remove_comments=False, remove_pis=False, strip_cdata=False)
    tree = etree.fromstring(xml_bytes, parser=parser).getroottree()
    root = tree.getroot()

    nb_conversions = 0
    for maplayer in root.iter("maplayer"):
        provider_el = maplayer.find("provider")
        if provider_el is None or (provider_el.text or "") not in PG_PROVIDERS:
            continue
        datasource_el = maplayer.find("datasource")
        if datasource_el is None or not (datasource_el.text or "").strip():
            continue
        layername_el = maplayer.find("layername")
        nom_couche = (layername_el.text if layername_el is not None else None) or maplayer.get("name", "")
        # Identifiant interne QGIS : stable et unique dans le projet, la ou deux
        # couches peuvent porter le meme nom.
        id_el = maplayer.find("id")
        id_couche = (id_el.text if id_el is not None else None) or ""

        result = convert_layer(datasource_el.text, service, exclude_hosts, dbnames)
        rows.append(
            {
                "chemin_projet": chemin_log,
                "id_couche": id_couche,
                "nom_couche": nom_couche,
                "statut": result.statut,
                "host_avant": result.host or "",
                "service": result.service or "",
                "datasource_avant": mask_password(result.avant),
                "datasource_apres": mask_password(result.apres),
            }
        )
        if result.statut == "converti":
            datasource_el.text = result.apres
            nb_conversions += 1

    if nb_conversions == 0:
        return None, 0
    new_bytes = etree.tostring(tree, xml_declaration=True, encoding="UTF-8", standalone=False)
    return new_bytes, nb_conversions


def process_qgs(
    path: Path,
    service: str,
    exclude_hosts: set[str],
    rows: list[dict[str, str]],
    dbnames: set[str] | None = None,
):
    return process_xml(path.read_bytes(), service, exclude_hosts, str(path), rows, dbnames)


def process_qgz(
    path: Path,
    service: str,
    exclude_hosts: set[str],
    rows: list[dict[str, str]],
    dbnames: set[str] | None = None,
):
    """Decompresse, convertit le .qgs interne, recompresse en preservant les
    pieces jointes du projet (.qgd notamment)."""
    with zipfile.ZipFile(path) as zin:
        infos = zin.infolist()
        qgs_names = [i.filename for i in infos if i.filename.lower().endswith(".qgs")]
        if not qgs_names:
            return None, 0
        qgs_name = qgs_names[0]
        contents = {info.filename: (info, zin.read(info.filename)) for info in infos}

    new_qgs_bytes, nb_conversions = process_xml(
        contents[qgs_name][1], service, exclude_hosts, f"{path}!{qgs_name}", rows, dbnames
    )
    if nb_conversions == 0:
        return None, 0

    contents[qgs_name] = (contents[qgs_name][0], new_qgs_bytes)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zout:
        for info, data in contents.values():
            new_info = zipfile.ZipInfo(filename=info.filename, date_time=info.date_time)
            new_info.compress_type = info.compress_type
            new_info.external_attr = info.external_attr
            zout.writestr(new_info, data)
    return buffer.getvalue(), nb_conversions


def nom_sauvegarde(path: Path, racine: Path | None) -> Path:
    """Chemin de la copie dans le dossier de sauvegarde. En mode dossier,
    l'arborescence est conservee. En mode liste, le chemin complet est aplati en
    un nom unique, pour que deux projets homonymes ne s'ecrasent pas."""
    if racine is not None:
        return path.relative_to(racine)
    plat = str(path.resolve()).replace(":", "").replace("\\", "__").replace("/", "__")
    return Path(plat.lstrip("_"))


def write_atomic(path: Path, data: bytes) -> None:
    """Ecrit via un fichier temporaire dans le meme dossier, puis remplace.
    Evite de laisser un projet tronque si l'ecriture est interrompue, ce qui
    est un risque reel sur un partage reseau."""
    tmp = path.with_name(path.name + ".tmp_pgservice")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except OSError:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise


def collecter_projets(racine: Path | None, liste: Path | None, backup_dir: Path) -> list[Path]:
    if liste is not None:
        chemins = []
        for ligne in liste.read_text(encoding="utf-8-sig").splitlines():
            ligne = ligne.strip()
            if not ligne or ligne.startswith("#"):
                continue
            p = Path(ligne)
            if not p.exists():
                print(f"  [!] introuvable, ignore : {p}", file=sys.stderr)
                continue
            chemins.append(p)
        return chemins
    # Les sauvegardes, de ce passage comme des precedents, ne sont jamais
    # retraitees : ce sont elles qui permettent le retour arriere.
    return sorted(
        p for p in list(racine.rglob("*.qgs")) + list(racine.rglob("*.qgz"))
        if BACKUP_DIRNAME not in p.parts and backup_dir not in p.parents
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("racine", type=Path, nargs="?", help="Dossier racine des projets QGIS (ex: L:\\SIG)")
    parser.add_argument("--liste", type=Path, help="Fichier texte listant les projets, un chemin par ligne")
    parser.add_argument("--service", default="prod", help="Nom du service pg_service.conf (defaut : prod)")
    parser.add_argument(
        "--exclude-host",
        action="append",
        default=[],
        metavar="IP",
        help="Host a ne pas convertir, repetable (ex: --exclude-host 203.0.113.10)",
    )
    parser.add_argument(
        "--dbname",
        dest="dbnames",
        action="append",
        default=[],
        metavar="NOM",
        help=(
            "Restreint la conversion aux couches dont le dbname= figure dans "
            "cette liste, repetable (ex: --dbname db_a). Utile quand "
            "plusieurs services designent des bases differentes sur un meme "
            "host : un passage par service, chacun filtre sur son dbname."
        ),
    )
    parser.add_argument("--apply", action="store_true", help="Ecrit reellement les fichiers (defaut : simulation)")
    parser.add_argument("--dry-run", action="store_true", help="Simulation explicite (comportement par defaut)")
    parser.add_argument("--backup-dir", type=Path, default=None, help="Dossier de sauvegarde (defaut en mode dossier : <racine>/_backup_pgservice)")
    parser.add_argument("--log", type=Path, default=Path("journal_conversion.csv"))
    args = parser.parse_args()

    if bool(args.racine) == bool(args.liste):
        sys.exit("Indiquer soit un dossier racine, soit --liste, pas les deux.")
    if args.apply and args.dry_run:
        sys.exit("--apply et --dry-run sont contradictoires : choisir l'un ou l'autre.")
    if args.liste and args.apply and not args.backup_dir:
        sys.exit(
            "En mode --liste, indiquer explicitement --backup-dir : les projets "
            "peuvent venir de plusieurs dossiers, il n'y a pas d'emplacement de "
            "sauvegarde evident."
        )
    apply_changes = args.apply
    exclude_hosts = set(args.exclude_host)
    dbnames = set(args.dbnames) or None

    horodatage = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = (args.backup_dir or (args.racine / BACKUP_DIRNAME)) / horodatage

    rows: list[dict[str, str]] = []
    n_projets_modifies = 0
    n_erreurs_lecture = 0
    echecs_ecriture: list[tuple[Path, str]] = []
    projets = collecter_projets(args.racine, args.liste, backup_dir)

    for path in projets:
        try:
            if path.suffix.lower() == ".qgz":
                new_bytes, nb_conv = process_qgz(path, args.service, exclude_hosts, rows, dbnames)
            else:
                new_bytes, nb_conv = process_qgs(path, args.service, exclude_hosts, rows, dbnames)
        except (etree.XMLSyntaxError, zipfile.BadZipFile, OSError) as exc:
            print(f"  [!] {path} : illisible ({exc.__class__.__name__}: {exc})", file=sys.stderr)
            n_erreurs_lecture += 1
            continue

        if nb_conv == 0:
            continue

        n_projets_modifies += 1
        if not apply_changes:
            print(f"  [ ] {path} : {nb_conv} couche(s) seraient converties (simulation)")
            continue

        # Sauvegarde puis ecriture, isolees : un projet ouvert par un agent
        # verrouille le fichier sous Windows. Un echec ne doit pas interrompre
        # le traitement des projets suivants.
        try:
            dest = backup_dir / nom_sauvegarde(path, args.racine)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(path.read_bytes())
            write_atomic(path, new_bytes)
        except OSError as exc:
            print(f"  [!] {path} : ecriture impossible ({exc.__class__.__name__}: {exc})", file=sys.stderr)
            echecs_ecriture.append((path, f"{exc.__class__.__name__}: {exc}"))
            n_projets_modifies -= 1
            continue
        print(f"  [x] {path} : {nb_conv} couche(s) convertie(s)")

    with args.log.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    par_statut: dict[str, int] = {}
    for row in rows:
        par_statut[row["statut"]] = par_statut.get(row["statut"], 0) + 1

    print(f"\n{len(projets)} fichiers projet parcourus ({n_erreurs_lecture} illisibles).")
    print(f"{n_projets_modifies} projet(s) {'convertis' if apply_changes else 'a convertir'}.")
    print(f"Service pose : {args.service}")
    if exclude_hosts:
        print(f"Hosts exclus : {', '.join(sorted(exclude_hosts))}")
    if dbnames:
        print(f"Bases visees : {', '.join(sorted(dbnames))}")
    print(f"Journal detaille : {args.log}\n")
    print("Repartition par statut de couche PostgreSQL :")
    for statut, n in sorted(par_statut.items(), key=lambda kv: -kv[1]):
        print(f"  {statut} : {n}")

    if echecs_ecriture:
        print(f"\n{len(echecs_ecriture)} projet(s) non ecrits (verrouilles ou inaccessibles) :")
        for path, motif in echecs_ecriture:
            print(f"  {path} : {motif}")
        print("Relancer le script sur ces projets une fois qu'ils sont fermes.")

    a_examiner = sum(par_statut.get(s, 0) for s in ("sans_host", "erreur_transformation"))
    if a_examiner:
        print(f"\n{a_examiner} couche(s) a examiner manuellement (voir statuts dans le journal).")

    if not apply_changes:
        print("\nSimulation uniquement, rien n'a ete ecrit. Relancer avec --apply pour convertir reellement.")
    elif n_projets_modifies:
        print(f"\nSauvegardes des originaux dans {backup_dir}")
    else:
        print("\nAucun projet a convertir : rien n'a ete modifie ni sauvegarde.")

    sys.exit(1 if echecs_ecriture else 0)


if __name__ == "__main__":
    main()
