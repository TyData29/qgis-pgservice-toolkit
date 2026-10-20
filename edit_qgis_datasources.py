#!/usr/bin/env python3
"""Modification en masse des sources PostgreSQL des couches de projets QGIS.

Deux operations, separees ou ensemble :

--schema ANCIEN=NOUVEAU (repetable, plusieurs anciens separes par des virgules) :
les couches qui lisent ANCIEN."table"
lisent NOUVEAU."table". Sert a passer d'un schema a un autre de meme
structure, par exemple d'un ancien millesime du cadastre au millesime courant.
Garde-fous :
- --tables-cible donne la liste des tables du schema cible (une par ligne,
  export CSV d'une colonne accepte) :
  une couche dont la table n'y figure pas est laissee intacte
  (statut table_absente_cible) ;
- un projet qui lit deja plusieurs schemas d'un meme regroupement (les anciens
  et le nouveau qui les remplace) est laisse intact pour cette operation
  (statut projet_multi_schemas) : il compare sans doute des millesimes, et le
  repointer melangerait ses couches.

--retirer-identifiants ROLE (repetable) : dans les couches connectees avec
user='ROLE', supprime user= et password=. QGIS utilise alors l'utilisateur
du service ou demande l'identifiant a l'ouverture du projet.

Le reste de la chaine de connexion est laisse intact. Seul le <datasource> de
chaque couche PostgreSQL est modifie.

Perimetre, au choix :
- un dossier entier (argument racine) ou une liste de projets (--liste, un
  chemin par ligne) : fichiers .qgs/.qgz ;
- --conn : projets stockes en base, dans les tables qgis_projects (voir
  convert_qgis_pgservice_db.py pour la connexion, la sauvegarde dans
  qgis_projects_bak et le retour arriere). --schema-projets et --projet
  (repetables) restreignent le parcours.

Usage :
    python edit_qgis_datasources.py <racine> --schema cadastre=cadastre_2025 --tables-cible tables.txt [--apply]
    python edit_qgis_datasources.py --liste panel.txt --retirer-identifiants userqlr --backup-dir DOSSIER [--apply]
    python edit_qgis_datasources.py --conn "service=prod" --schema ancien=nouveau --tables-cible tables.txt [--apply]

Le mode par defaut est une simulation : rien n'est ecrit.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import re
import sys
import zipfile
from pathlib import Path

from convert_qgis_pgservice import (
    PG_PROVIDERS,
    TOKEN_RE,
    collecter_projets,
    etree,
    get_param,
    mask_password,
    nom_sauvegarde,
    write_atomic,
)

BACKUP_DIRNAME = "_backup_edit_sources"
TABLE_RE = re.compile(r'table="(?P<schema>[^"]*)"\."(?P<table>[^"]*)"')
ZIP_SIGNATURE = b"PK\x03\x04"

LOG_FIELDS = ["chemin_projet", "id_couche", "nom_couche", "statut", "datasource_avant", "datasource_apres"]


class Regles:
    """Operations demandees. `schemas` : ancien -> nouveau ; `tables_cible` :
    nouveau -> tables existantes ; `roles` : roles dont on retire les
    identifiants."""

    def __init__(self, schemas: dict[str, str], tables_cible: dict[str, set[str]], roles: set[str]):
        self.schemas = schemas
        self.tables_cible = tables_cible
        self.roles = roles

    def regroupement(self, schema: str) -> str | None:
        """Schema cible du regroupement auquel appartient `schema`, ou None."""
        if schema in self.schemas:
            return self.schemas[schema]
        if schema in self.schemas.values():
            return schema
        return None


def retirer_identifiants(datasource: str) -> str:
    """Supprime user= et password=, sans toucher au reste."""
    tokens = [m for m in TOKEN_RE.finditer(datasource) if m.group("key") in ("user", "password")]
    for m in sorted(tokens, key=lambda m: m.start(), reverse=True):
        datasource = datasource[: m.start("ws")] + datasource[m.end("val") :]
    return datasource.strip()


def editer_couche(datasource: str, regles: Regles, schemas_bloques: set[str]) -> tuple[str, list[str]]:
    """Retourne (nouvelle_source, statuts). Aucun statut : couche non concernee."""
    statuts: list[str] = []
    nouveau = datasource

    m = TABLE_RE.search(datasource)
    if regles.schemas and m and m.group("schema") in regles.schemas:
        ancien, table = m.group("schema"), m.group("table")
        cible = regles.schemas[ancien]
        if cible in schemas_bloques:
            statuts.append("projet_multi_schemas")
        elif table not in regles.tables_cible.get(cible, set()):
            statuts.append("table_absente_cible")
        else:
            nouveau = nouveau[: m.start()] + f'table="{cible}"."{table}"' + nouveau[m.end() :]
            statuts.append("schema_change")

    if regles.roles and get_param(datasource, "user") in regles.roles:
        nouveau = retirer_identifiants(nouveau)
        statuts.append("identifiants_retires")

    return nouveau, statuts


def editer_xml(xml_bytes: bytes, regles: Regles, chemin_log: str, rows: list[dict[str, str]]):
    """Retourne (nouveaux_bytes_ou_None, nb_couches_modifiees)."""
    parser = etree.XMLParser(remove_comments=False, remove_pis=False, strip_cdata=False)
    tree = etree.fromstring(xml_bytes, parser=parser).getroottree()

    couches = []
    for maplayer in tree.getroot().iter("maplayer"):
        provider_el = maplayer.find("provider")
        datasource_el = maplayer.find("datasource")
        if provider_el is None or (provider_el.text or "") not in PG_PROVIDERS:
            continue
        if datasource_el is None or not (datasource_el.text or "").strip():
            continue
        couches.append((maplayer, datasource_el))

    # Regroupements dont le projet lit deja plusieurs schemas : pas de repointage.
    lus: dict[str, set[str]] = {}
    for _, datasource_el in couches:
        m = TABLE_RE.search(datasource_el.text)
        if m and regles.regroupement(m.group("schema")):
            lus.setdefault(regles.regroupement(m.group("schema")), set()).add(m.group("schema"))
    bloques = {cible for cible, schemas in lus.items() if len(schemas) > 1}

    nb = 0
    for maplayer, datasource_el in couches:
        avant = datasource_el.text
        apres, statuts = editer_couche(avant, regles, bloques)
        if not statuts:
            continue
        layername_el = maplayer.find("layername")
        id_el = maplayer.find("id")
        rows.append({
            "chemin_projet": chemin_log,
            "id_couche": (id_el.text if id_el is not None else None) or "",
            "nom_couche": (layername_el.text if layername_el is not None else None) or maplayer.get("name", ""),
            "statut": "+".join(statuts),
            "datasource_avant": mask_password(avant),
            "datasource_apres": mask_password(apres),
        })
        if apres != avant:
            datasource_el.text = apres
            nb += 1

    if nb == 0:
        return None, 0
    return etree.tostring(tree, xml_declaration=True, encoding="UTF-8", standalone=False), nb


def editer_qgz(data: bytes, regles: Regles, chemin_log: str, rows: list[dict[str, str]]):
    """Decompresse, modifie le .qgs interne, recompresse en preservant les
    pieces jointes du projet (.qgd notamment)."""
    with zipfile.ZipFile(io.BytesIO(data)) as zin:
        infos = zin.infolist()
        qgs_names = [i.filename for i in infos if i.filename.lower().endswith(".qgs")]
        if not qgs_names:
            return None, 0
        qgs_name = qgs_names[0]
        contents = {info.filename: (info, zin.read(info.filename)) for info in infos}

    new_qgs, nb = editer_xml(contents[qgs_name][1], regles, f"{chemin_log}!{qgs_name}", rows)
    if nb == 0:
        return None, 0
    contents[qgs_name] = (contents[qgs_name][0], new_qgs)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zout:
        for info, contenu in contents.values():
            new_info = zipfile.ZipInfo(filename=info.filename, date_time=info.date_time)
            new_info.compress_type = info.compress_type
            new_info.external_attr = info.external_attr
            zout.writestr(new_info, contenu)
    return buffer.getvalue(), nb


def traiter_fichiers(args, regles: Regles, rows: list[dict[str, str]]) -> tuple[int, int, list]:
    horodatage = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    base_sauvegarde = args.backup_dir or (args.racine / BACKUP_DIRNAME if args.racine else None)
    backup_dir = base_sauvegarde / horodatage if base_sauvegarde else None
    projets = [p for p in collecter_projets(args.racine, args.liste, backup_dir) if BACKUP_DIRNAME not in p.parts]

    n_modifies, echecs = 0, []
    for path in projets:
        try:
            data = path.read_bytes()
            if path.suffix.lower() == ".qgz":
                new_bytes, nb = editer_qgz(data, regles, str(path), rows)
            else:
                new_bytes, nb = editer_xml(data, regles, str(path), rows)
        except (etree.XMLSyntaxError, zipfile.BadZipFile, OSError) as exc:
            print(f"  [!] {path} : illisible ({exc.__class__.__name__}: {exc})", file=sys.stderr)
            continue
        if nb == 0:
            continue
        n_modifies += 1
        if not args.apply:
            print(f"  [ ] {path} : {nb} couche(s) seraient modifiees (simulation)")
            continue
        try:
            dest = backup_dir / nom_sauvegarde(path, args.racine)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            write_atomic(path, new_bytes)
        except OSError as exc:
            print(f"  [!] {path} : ecriture impossible ({exc.__class__.__name__}: {exc})", file=sys.stderr)
            echecs.append((str(path), f"{exc.__class__.__name__}: {exc}"))
            n_modifies -= 1
            continue
        print(f"  [x] {path} : {nb} couche(s) modifiee(s)")
    if args.apply and n_modifies:
        print(f"\nSauvegardes des originaux dans {backup_dir}")
    return len(projets), n_modifies, echecs


def traiter_base(args, regles: Regles, rows: list[dict[str, str]]) -> tuple[int, int, list]:
    from convert_qgis_pgservice_db import (
        TABLE_PROJETS,
        TABLE_SAUVEGARDE,
        connecter,
        creer_table_sauvegarde,
        lister_projets,
        psycopg,
        schemas_avec_projets,
        sql,
    )

    lot = f"{dt.datetime.now():%Y%m%d_%H%M%S}_edit_sources"
    conn = connecter(args.conn, args.demander_mdp)
    n_projets, n_modifies, echecs = 0, 0, []
    try:
        with conn.cursor() as cur:
            cur.execute("set lock_timeout = '10s'")
            # Contexte reel de la session : un service ou un reglage de role (ALTER ROLE ... SET role)
            # peut mener ailleurs que prevu.
            cur.execute("select inet_server_addr(), inet_server_port(), current_database(), session_user, current_user")
            hote, port, base, connexion, actif = cur.fetchone()
        conn.commit()
        print(f"Connecte a {hote}:{port}, base {base}, en tant que {connexion} (role actif : {actif})")
        disponibles = schemas_avec_projets(conn)
        print(f"Tables {TABLE_PROJETS} trouvees dans : {', '.join(disponibles) or 'aucun schema'}")
        schemas = args.schemas_projets or disponibles
        absents = [s for s in schemas if s not in disponibles]
        if absents:
            sys.exit(f"Pas de table {TABLE_PROJETS} lisible dans : {', '.join(absents)}")
        for schema in schemas:
            # Une table qgis_projects illisible pour ce role (droit SELECT absent) est signalee
            # et sautee : les autres schemas sont traites.
            try:
                noms = [n for n in lister_projets(conn, schema) if not args.projets or n in args.projets]
            except psycopg.Error as exc:
                conn.rollback()
                print(f"  [!] {schema}.{TABLE_PROJETS} : lecture refusee, schema ignore ({str(exc).strip()})", file=sys.stderr)
                echecs.append((f"{schema}.{TABLE_PROJETS}", f"lecture refusee : {str(exc).strip()}"))
                continue
            if noms and args.apply:
                creer_table_sauvegarde(conn, schema)
            projets = sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(TABLE_PROJETS))
            sauvegarde = sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(TABLE_SAUVEGARDE))
            for nom in noms:
                n_projets += 1
                etiquette = f"{schema}.{TABLE_PROJETS}/{nom}"
                try:
                    with conn.cursor() as cur:
                        lecture = "select content from {} where name = %s" + (" for update" if args.apply else "")
                        cur.execute(sql.SQL(lecture).format(projets), (nom,))
                        ligne = cur.fetchone()
                        contenu = bytes(ligne[0]) if ligne and ligne[0] is not None else b""
                        if not contenu.startswith(ZIP_SIGNATURE):
                            conn.rollback()
                            continue
                        try:
                            new_bytes, nb = editer_qgz(contenu, regles, etiquette, rows)
                        except (etree.XMLSyntaxError, zipfile.BadZipFile) as exc:
                            print(f"  [!] {etiquette} : illisible ({exc})", file=sys.stderr)
                            conn.rollback()
                            continue
                        if nb == 0 or not args.apply:
                            conn.rollback()
                            if nb:
                                n_modifies += 1
                                print(f"  [ ] {etiquette} : {nb} couche(s) seraient modifiees (simulation)")
                            continue
                        cur.execute(
                            sql.SQL("insert into {} (name, metadata, content, lot) "
                                    "select name, metadata, content, %s from {} where name = %s").format(sauvegarde, projets),
                            (lot, nom),
                        )
                        cur.execute(sql.SQL("update {} set content = %s where name = %s").format(projets), (new_bytes, nom))
                    conn.commit()
                    n_modifies += 1
                    print(f"  [x] {etiquette} : {nb} couche(s) modifiee(s)")
                except psycopg.Error as exc:
                    conn.rollback()
                    print(f"  [!] {etiquette} : ecriture impossible ({exc})", file=sys.stderr)
                    echecs.append((etiquette, str(exc).strip()))
    finally:
        conn.close()
    if args.apply and n_modifies:
        print(f"\nOriginaux sauvegardes dans <schema>.{TABLE_SAUVEGARDE}, lot {lot}")
    return n_projets, n_modifies, echecs


def lire_regles(args) -> Regles:
    schemas: dict[str, str] = {}
    for paire in args.schema:
        anciens, sep, nouveau = paire.partition("=")
        noms = [a.strip() for a in anciens.split(",") if a.strip()]
        if not sep or not noms or not nouveau or nouveau in noms:
            sys.exit(f"--schema attend ANCIEN=NOUVEAU ou ANCIEN1,ANCIEN2=NOUVEAU : {paire!r}")
        for ancien in noms:
            schemas[ancien] = nouveau
    tables: dict[str, set[str]] = {}
    if schemas:
        if not args.tables_cible:
            sys.exit("--schema demande --tables-cible : la liste des tables du schema cible, une par ligne.")
        # Un nom par ligne ; les guillemets d'un export CSV (pgAdmin) sont ignores.
        noms = {l.strip().strip('"') for l in args.tables_cible.read_text(encoding="utf-8-sig").splitlines() if l.strip()}
        tables = {cible: noms for cible in set(schemas.values())}
        if len(set(schemas.values())) > 1:
            sys.exit("Un seul schema cible par passage : --tables-cible ne decrit qu'un schema.")
    return Regles(schemas, tables, set(args.retirer_identifiants))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("racine", type=Path, nargs="?", help="Dossier racine des projets QGIS")
    parser.add_argument("--liste", type=Path, help="Fichier texte listant les projets, un chemin par ligne")
    parser.add_argument("--conn", help="Projets stockes en base : chaine de connexion libpq (ex: \"service=prod\")")
    parser.add_argument("-W", "--password", dest="demander_mdp", action="store_true", help="Avec --conn : demande le mot de passe")
    parser.add_argument("--schema-projets", dest="schemas_projets", action="append", default=[], metavar="NOM",
                        help="Avec --conn : schema contenant une table qgis_projects, repetable (defaut : tous)")
    parser.add_argument("--projet", dest="projets", action="append", default=[], metavar="NOM",
                        help="Avec --conn : nom de projet a traiter, repetable (defaut : tous)")
    parser.add_argument("--schema", action="append", default=[], metavar="ANCIEN[,ANCIEN2]=NOUVEAU",
                        help="Repointe les couches du schema ANCIEN vers NOUVEAU, repetable")
    parser.add_argument("--tables-cible", type=Path, help="Tables existantes du schema cible, une par ligne")
    parser.add_argument("--retirer-identifiants", action="append", default=[], metavar="ROLE",
                        help="Supprime user= et password= des couches connectees avec ce role, repetable")
    parser.add_argument("--apply", action="store_true", help="Ecrit reellement (defaut : simulation)")
    parser.add_argument("--backup-dir", type=Path, default=None,
                        help=f"Fichiers : dossier de sauvegarde (defaut en mode dossier : <racine>/{BACKUP_DIRNAME})")
    parser.add_argument("--log", type=Path, default=Path("journal_edit_sources.csv"))
    args = parser.parse_args()

    if sum(bool(x) for x in (args.racine, args.liste, args.conn)) != 1:
        sys.exit("Indiquer un seul perimetre : un dossier racine, --liste ou --conn.")
    if not args.schema and not args.retirer_identifiants:
        sys.exit("Rien a faire : indiquer --schema et/ou --retirer-identifiants.")
    if args.liste and args.apply and not args.backup_dir:
        sys.exit("En mode --liste, indiquer explicitement --backup-dir.")
    regles = lire_regles(args)

    rows: list[dict[str, str]] = []
    if args.conn:
        n_projets, n_modifies, echecs = traiter_base(args, regles, rows)
    else:
        n_projets, n_modifies, echecs = traiter_fichiers(args, regles, rows)

    with args.log.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    par_statut: dict[str, int] = {}
    for row in rows:
        par_statut[row["statut"]] = par_statut.get(row["statut"], 0) + 1
    print(f"\n{n_projets} projet(s) parcourus, {n_modifies} {'modifie(s)' if args.apply else 'a modifier'}.")
    print(f"Journal detaille : {args.log}\n")
    print("Repartition par statut de couche :")
    for statut, n in sorted(par_statut.items(), key=lambda kv: -kv[1]):
        print(f"  {statut} : {n}")
    if echecs:
        print(f"\n{len(echecs)} projet(s) non ecrits (ouverts ou inaccessibles) :")
        for etiquette, motif in echecs:
            print(f"  {etiquette} : {motif}")
        print("Relancer le script sur ces projets une fois qu'ils sont fermes.")
    if not args.apply:
        print("\nSimulation uniquement, rien n'a ete ecrit. Relancer avec --apply pour modifier reellement.")
    sys.exit(1 if echecs else 0)


if __name__ == "__main__":
    main()
