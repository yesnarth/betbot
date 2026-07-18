# Déploiement BetBot sur le VPS PARTAGÉ ESARTH

> Cible : VPS OVH `91.134.240.150` (Ubuntu 25.04), qui héberge déjà ~15 conteneurs
> de prod derrière le reverse proxy **partagé** `esarth-nginx-prod`.
> **Zéro downtime** pour les sites existants : on ne touche jamais aux ports 80/443
> ni ne redémarre nginx — on ajoute un server block et on `nginx -s reload`.
>
> Modèle suivi : `mytrader` (bot Python déployé de la même façon — `trade.rboost.link`).
> Ce runbook diffère de `DEPLOY_OVH.md` (kit Caddy pour VPS vierge — NON applicable ici).

## Convention ESARTH respectée
- **Image pré-construite** sur le PC → `docker load` (AUCUN build sur le VPS : disque 72 Go à 72 %).
- **Pas de proxy embarqué** : seul `betbot-dashboard-prod` joint `esarth-proxy-net` ; le reste est privé (`betbot_net`, bind `127.0.0.1`).
- **HTTPS** via certbot **webroot** (`/var/www/certbot`) — pas d'arrêt de nginx.
- **Auth** : Basic Auth nginx (`htpasswd-pronos`) car Streamlit n'a pas d'auth native.

---

## 0. Prérequis (À TA CHARGE)
1. **DNS** : créer un enregistrement **A** `pronos.rboost.link` → `91.134.240.150`
   (chez le gestionnaire DNS de `rboost.link`). Vérifier : `nslookup pronos.rboost.link`.
   → Sous-domaine modifiable ; adapter partout ci-dessous + dans `infra/nginx/betbot.conf`.
2. **`.env` prod** dans `betbot/` : `POSTGRES_PASSWORD` fort, `ODDS_API_KEY`,
   `FOOTBALL_DATA_API_KEY`, `GMAIL_*`, `BANKROLL`, etc. (jamais commité, jamais dans l'image).
3. **Docker Desktop** lancé sur le PC.

Variables utilisées ci-dessous :
```
SUB=pronos.rboost.link        # sous-domaine
SUBDIR=pronos-rboost          # dossier certs (convention trade-rboost)
KEY="C:\Users\HP\.ssh\fne_vps_key"
```

---

## 1. Construire + exporter l'image (PC, PowerShell, dans betbot/)
```powershell
docker build -t betbot:latest .
docker save betbot:latest | gzip > betbot-image.tar.gz     # ~400-600 Mo
```

## 2. Transférer vers le VPS
```powershell
& "C:\Windows\System32\OpenSSH\ssh.exe" -i $KEY ubuntu@91.134.240.150 "mkdir -p /home/ubuntu/betbot/scripts /home/ubuntu/betbot/backups"
scp -i $KEY betbot-image.tar.gz docker-compose.vps.yml .env ubuntu@91.134.240.150:/home/ubuntu/betbot/
scp -i $KEY scripts/backup_db.sh ubuntu@91.134.240.150:/home/ubuntu/betbot/scripts/
```

## 3. Charger l'image + démarrer la stack (VPS)
```bash
ssh -i "$KEY" ubuntu@91.134.240.150
cd /home/ubuntu/betbot
docker load < betbot-image.tar.gz
docker compose -f docker-compose.vps.yml up -d
docker compose -f docker-compose.vps.yml ps          # tous "healthy" ; migrate = exited(0)
docker compose -f docker-compose.vps.yml logs -f worker   # Ctrl-C quand le scheduler tourne
```

## 4. GATE CRITIQUE — nginx voit-il le dashboard ?
> Ne JAMAIS ajouter le server block tant que ceci ne renvoie pas `ok`
> (un upstream non résolvable fait tomber les 8 sites au prochain reload).
```bash
docker exec esarth-nginx-prod wget -qO- http://betbot-dashboard-prod:8501/_stcore/health && echo " <- OK"
```

## 5. Certificat TLS (webroot — zéro downtime)
```bash
sudo certbot certonly --webroot -w /var/www/certbot -d pronos.rboost.link
# → /etc/letsencrypt/live/pronos.rboost.link/
sudo mkdir -p /home/ubuntu/nginx-proxy/certs/pronos-rboost
sudo cp /etc/letsencrypt/live/pronos.rboost.link/fullchain.pem /home/ubuntu/nginx-proxy/certs/pronos-rboost/
sudo cp /etc/letsencrypt/live/pronos.rboost.link/privkey.pem   /home/ubuntu/nginx-proxy/certs/pronos-rboost/
sudo chown -R ubuntu:ubuntu /home/ubuntu/nginx-proxy/certs/pronos-rboost
```
**Renouvellement** : vérifier que le deploy-hook certbot copie ce cert vers le dossier
nginx (sinon il expirera dans 90 j) :
```bash
ls /etc/letsencrypt/renewal-hooks/deploy/
# S'assurer que le hook copie /etc/letsencrypt/live/<domaine>/ → certs/<subdir>/ pour BetBot aussi.
```

## 6. Mot de passe (Basic Auth)
```bash
printf 'betbot:%s\n' "$(openssl passwd -apr1 'TON_MOT_DE_PASSE_FORT')" \
  > /home/ubuntu/nginx-proxy/certs/htpasswd-pronos
```

## 7. Ajouter le server block + recharger nginx
```bash
cp /home/ubuntu/nginx-proxy/nginx.conf /home/ubuntu/nginx-proxy/nginx.conf.bak.before-betbot-$(date -u +%Y%m%d_%H%M%S)
# Coller le contenu de infra/nginx/betbot.conf JUSTE AVANT la dernière accolade } du bloc http{}
#   (ex. avec: sudo nano /home/ubuntu/nginx-proxy/nginx.conf)
docker exec esarth-nginx-prod nginx -t          # DOIT afficher "syntax is ok / test is successful"
docker exec esarth-nginx-prod nginx -s reload    # zéro downtime — ne recree PAS le conteneur
```
> Si `nginx -t` échoue → NE PAS reload. Restaurer le `.bak` et corriger.

## 8. Vérifier
```bash
curl -sk -u betbot:TON_MOT_DE_PASSE https://pronos.rboost.link/_stcore/health   # -> ok
```
Puis dans le navigateur : `https://pronos.rboost.link` → login Basic Auth → dashboard.

---

## Rollback (si un site casse)
```bash
cp /home/ubuntu/nginx-proxy/nginx.conf.bak.before-betbot-* /home/ubuntu/nginx-proxy/nginx.conf
docker exec esarth-nginx-prod nginx -t && docker exec esarth-nginx-prod nginx -s reload
```

## Mise à jour ultérieure (nouveau code / nouvelle clé Odds)
```powershell
# PC :
docker build -t betbot:latest . ; docker save betbot:latest | gzip > betbot-image.tar.gz
scp -i $KEY betbot-image.tar.gz .env ubuntu@91.134.240.150:/home/ubuntu/betbot/
```
```bash
# VPS :
cd /home/ubuntu/betbot
docker load < betbot-image.tar.gz
docker compose -f docker-compose.vps.yml up -d          # recree les conteneurs avec la nouvelle image
docker image prune -f                                    # ménage (JAMAIS --volumes)
```
> Clé Odds tous les 4 jours : éditer `.env` (scp) puis
> `docker compose -f docker-compose.vps.yml up -d --force-recreate worker api` suffit.

## Accès BDD / API pour debug (tunnel SSH, sans exposition publique)
```powershell
ssh -i $KEY -L 8000:127.0.0.1:8000 -L 8501:127.0.0.1:8501 ubuntu@91.134.240.150
# puis http://localhost:8000/docs (API) et http://localhost:8501 (dashboard) en local
```
