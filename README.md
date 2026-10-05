# qgis-pgservice-toolkit

Deux scripts Python pour faire migrer en masse des projets QGIS (`.qgs`/`.qgz`) des parametres de connexion PostgreSQL en dur (`host=`/`port=`/`dbname=`) vers un `pg_service.conf` centralise, et pour basculer ensuite ces projets entre deux services le temps d'une migration a blanc.

Ecrits pour un contexte concret : une collectivite ou une PME avec un parc de projets QGIS geres par une petite equipe, ou changer le serveur cible pour 40 projets a la main n'est pas envisageable. Le principe : le projet QGIS ne pointe plus vers `host=X port=Y dbname=Z`, mais vers `service='nom'`, dont la resolution (host, port, dbname) vit dans `pg_service.conf` sur le poste de chaque agent. Basculer un serveur revient alors a changer un fichier, pas quarante projets.

## Scripts

### convert_qgis_pgservice.py

Remplace `host=`/`port=`/`dbname=` par `service='<nom>'` dans chaque couche PostgreSQL/PostGIS d'un projet QGIS. Le reste de la chaine de connexion (`user=`, `password=`, `sslmode=`, `table=`, `key=`, `srid=`, `type=`, `sql=`...) est laisse strictement intact.

```
python convert_qgis_pgservice.py <racine> [--service prod] [--exclude-host IP] [--dbname NOM] [--apply] [--backup-dir DOSSIER] [--log FICHIER]
python convert_qgis_pgservice.py --liste panel.txt --backup-dir DOSSIER [--apply] ...
```

Idempotent : une couche deja convertie (`service=` present) est laissee de cote. Une couche en `authcfg=` est signalee pour traitement manuel. `--dbname` (repetable) restreint la conversion aux couches dont le `dbname=` figure dans la liste, ce qui permet un passage par service quand plusieurs services designent des bases differentes sur un meme host.

### switch_qgis_service.py

Bascule les projets d'un nom de service a un autre, dans les deux sens. Sert a tester une migration sur un panel de projets avant de la generaliser, puis a revenir en arriere si besoin.

```
python switch_qgis_service.py <racine> --from prod --to prod_test [--apply] [--backup-dir DOSSIER] [--log FICHIER]
python switch_qgis_service.py --liste panel.txt --from prod --to prod_test --backup-dir DOSSIER [--apply] [--log FICHIER]
```

Avec `<racine>`, les sauvegardes vont par defaut dans `<racine>/_backup_switch_service/<horodatage>`. Avec `--liste`, les projets peuvent etre disperses : `--backup-dir` est alors obligatoire des que `--apply` est utilise (inutile en simulation).

Une couche qui porte deja le service cible est laissee telle quelle : relancer le script deux fois de suite ne produit rien de plus.

## Points communs

- **Simulation par defaut.** Rien n'est ecrit sur le disque sans `--apply`.
- **Sauvegarde systematique.** Avant toute ecriture, l'original est copie dans un dossier horodate (`_backup_pgservice` ou `_backup_switch_service`).
- **Ecriture atomique.** Passage par un fichier temporaire puis remplacement, pour ne jamais laisser un projet tronque si l'ecriture est interrompue (utile sur un partage reseau).
- **Journal CSV.** Chaque execution produit un journal detaille couche par couche (statut, avant/apres, mot de passe masque).
- **Perimetre au choix.** Un dossier entier parcouru recursivement, ou une liste explicite de projets (`--liste`, un chemin par ligne) pour cibler un panel de test.
- **XML preserve fidelement.** Usage de `lxml` plutot que `xml.etree.ElementTree`, pour ne pas perdre les commentaires et instructions de traitement du fichier projet d'origine.

## Prerequis

```
pip install lxml
```

Python 3.10 ou plus (usage de `X | None` dans les annotations de type).

## Licence

MIT, voir [LICENSE](LICENSE).
