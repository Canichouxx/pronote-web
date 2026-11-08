"""Version web (ordi) de l'app Pronote : Flask + pronotepy. Design sombre, icônes animées."""
import json
import os
import random
import secrets
import threading
import uuid
from datetime import date, datetime
from urllib.parse import quote, unquote, urlparse

from flask import Flask, Response, redirect, render_template_string, request, session

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config.update(MAX_CONTENT_LENGTH=8 * 1024 * 1024, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER") or os.environ.get("HTTPS_ONLY")))

SESSIONS = {}  # sid -> {"creds": {...}, "data": {...}, "lock": Lock}  (en mémoire : rien n'est écrit sur disque)
SITE_PASSWORD = os.environ.get("SITE_PASSWORD")  # optionnel : protège le site par un mot de passe


@app.before_request
def protect():
    if SITE_PASSWORD:
        a = request.authorization
        if not (a and secrets.compare_digest(a.password or "", SITE_PASSWORD)):
            return Response("Mot de passe du site requis", 401, {"WWW-Authenticate": 'Basic realm="Pronote"'})


# ---------- Connexion Pronote ----------
def clean_url(url):
    """Accepte n'importe quel lien donné par le lycée et en sort .../pronote/eleve.html"""
    url = "".join(url.strip().strip("\"'<>").split())
    if "service=" in url:  # lien EduConnect : https://.../cas/login?service=<lien Pronote>
        url = unquote(url.split("service=", 1)[1].split("&")[0])
    if "://" not in url:
        url = "https://" + url
    url = "https://" + url.split("://", 1)[1]
    url = url.split("?")[0].split("#")[0]
    url = url.replace("mobile.eleve.html", "eleve.html").replace("mobile.parent.html", "parent.html")
    if "eleve.html" not in url and "parent.html" not in url:
        p = urlparse(url)
        url = f"{p.scheme}://{p.netloc}{p.path.rstrip('/') or '/pronote'}/eleve.html"
    return url


def friendly(e):
    s = str(e)
    if "Page html is different" in s:
        return ("Le lien ne mène pas à la page de connexion Pronote (ton lycée utilise sûrement EduConnect). "
                "Choisis EduConnect ou le QR code.")
    if "probably wrong login information" in s:
        return "Identifiant ou mot de passe EduConnect incorrect."
    if "HubEduConnect" in s:
        return "Ton lycée n'est pas compatible avec cette connexion EduConnect. Utilise la méthode QR code."
    if "Connection failure" in s:
        return ("La connexion EduConnect automatique n'a pas abouti (ton lycée n'utilise sûrement pas ce chemin). "
                "Utilise la méthode QR code.")
    if "IP address is suspended" in s:
        return "Pronote a bloqué l'adresse IP du serveur un moment. Réessaie plus tard."
    if "MFA" in type(e).__name__:
        return "Ton compte demande une double authentification (pas gérée)."
    if "QR" in type(e).__name__ or "decrypt" in s.lower():
        return "QR code ou code à 4 chiffres incorrect (ou QR code expiré). Génère-en un nouveau."
    if "Connection" in type(e).__name__ or "Max retries" in s:
        return "Impossible de joindre Pronote : vérifie le lien."
    return s[:220] or type(e).__name__


def decode_qr_image(file):
    """Lit le QR code d'une capture d'écran envoyée par l'utilisateur."""
    try:
        import zxingcpp
        from PIL import Image
        img = Image.open(file.stream).convert("RGB")
        found = zxingcpp.read_barcodes(img)
    except Exception:
        raise Exception("Impossible de lire cette image. Envoie une capture d'écran (PNG ou JPG).")
    if not found:
        raise Exception("Aucun QR code lisible sur l'image : envoie une capture nette, avec le QR code en entier.")
    return found[0].text


def login(mode, f, device_uuid, files=None):
    import pronotepy
    import pronotepy.ent as ent_mod
    if mode == "qr":
        text = f.get("qr", "").strip()
        if not text and files and files.get("qrimg") and files["qrimg"].filename:
            text = decode_qr_image(files["qrimg"])
        try:
            qr = json.loads(text)
            qr["jeton"], qr["login"], qr["url"]
        except Exception:
            raise Exception("Contenu du QR code invalide : colle tout le texte lu dans le QR code.")
        pin = f.get("pin", "").strip()
        if not (pin.isdigit() and len(pin) == 4):
            raise Exception("Le code doit faire exactement 4 chiffres.")
        return pronotepy.Client.qrcode_login(qr, pin, device_uuid)
    url = clean_url(f.get("url", ""))
    if "parent.html" in url:
        raise Exception("Ce lien est celui d'un compte parent : le site gère seulement les comptes élèves.")
    if mode == "edu":
        ent = getattr(ent_mod, "bordeaux", None) or ent_mod.pronote_hubeduconnect(url)  # hub EduConnect d'Index Education
    else:
        name = f.get("ent", "")
        ent = getattr(ent_mod, name) if name else None
    c = pronotepy.Client(url, username=f.get("user", "").strip(), password=f.get("pwd", ""), ent=ent)
    if not c.logged_in:
        raise Exception("Identifiants refusés")
    try:  # on échange la connexion contre un jeton : le mot de passe n'est pas gardé
        pin = "".join(random.choice("123456789") for _ in range(4))
        c = pronotepy.Client.qrcode_login(c.request_qr_code_data(pin), pin, device_uuid)
    except Exception:
        pass
    return c


def fetch(c):
    to_num = lambda g: float(str(g.grade).replace(",", ".")) / float(str(g.out_of).replace(",", ".")) * 20
    grades = []
    for g in c.current_period.grades:
        try:
            n = to_num(g)
        except Exception:
            n = None
        grades.append({"subject": g.subject.name, "date": g.date.strftime("%d/%m"), "value": f"{g.grade}/{g.out_of}" if n is not None else str(g.grade), "n": n})
    nums = [g["n"] for g in grades if g["n"] is not None]
    return {
        "name": c.info.name,
        "grades": grades,
        "average": round(sum(nums) / len(nums), 2) if nums else None,
        "homework": [{"subject": h.subject.name, "text": h.description, "date": h.date.strftime("%d/%m")}
                     for h in c.homework(date.today())],
        "lessons": [{"subject": l.subject.name if l.subject else "Cours", "room": l.room or "",
                     "time": l.start.strftime("%H:%M")} for l in c.lessons(date.today())],
        "at": datetime.now().strftime("%H:%M"),
    }


def current():
    return SESSIONS.get(session.get("sid"))


# ---------- Pages ----------
@app.get("/")
def home():
    s = current()
    if not s:
        sel = request.args.get("m", "edu")
        return render_template_string(PAGE, logged=False, error=request.args.get("e"), ents=ent_names(),
                                      sel=sel if sel in ("edu", "qr", "pwd") else "edu")
    return render_template_string(PAGE, logged=True, d=s["data"])


@app.post("/login")
def do_login():
    mode = request.form.get("mode", "edu")
    try:
        dev = session.get("dev") or uuid.uuid4().hex
        session["dev"] = dev
        c = login(mode, request.form, dev, request.files)
        data = fetch(c)
        sid = secrets.token_hex(16)
        SESSIONS[sid] = {"creds": c.export_credentials(), "data": data, "lock": threading.Lock()}
        session["sid"] = sid
        return redirect("/")
    except Exception as e:
        msg = friendly(e)
        sel = "qr" if ("méthode QR code" in msg and mode != "qr") else mode  # on bascule sur le QR code si c'est la solution
        return redirect("/?m=" + sel + "&e=" + quote(msg))


@app.post("/refresh")
def refresh():
    import pronotepy
    s = current()
    if s:
        with s["lock"]:
            try:
                c = pronotepy.Client.token_login(**s["creds"])
                s["creds"] = c.export_credentials()  # le jeton change à chaque connexion
                s["data"] = fetch(c)
            except Exception as e:
                SESSIONS.pop(session.pop("sid", None), None)
                return redirect("/?m=qr&e=" + quote("Session expirée, reconnecte-toi. (" + friendly(e)[:80] + ")"))
    return redirect("/")


@app.post("/logout")
def logout():
    SESSIONS.pop(session.pop("sid", None), None)
    return redirect("/")


def ent_names():
    try:
        import pronotepy.ent as e
        return sorted(n for n in dir(e) if not n.startswith("_") and callable(getattr(e, n)))
    except Exception:
        return []


ICONS = {
    "home": '<path d="M3 11l9-7 9 7"/><path d="M5 10v10h14V10"/>',
    "grades": '<path d="M6 20V13"/><path d="M12 20V7"/><path d="M18 20V3"/>',
    "homework": '<rect x="4" y="3" width="16" height="18" rx="3"/><path d="M8.5 12l2.5 2.5 4.5-5"/>',
    "edt": '<rect x="3" y="5" width="18" height="16" rx="3"/><path d="M3 10h18M8 3v4M16 3v4"/>',
}

PAGE = r"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Pronote</title>
<style>
:root{--bg:#0d0d12;--card:#1a1a24;--acc:#786bff;--grey:#73738a;--txt:#ececf5;color-scheme:dark}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);font:16px system-ui,sans-serif}
h1{font-size:28px;margin:0 0 18px}.sub{color:var(--grey);font-size:13px}
.wrap{max-width:560px;margin:0 auto;padding:22px 18px 100px}
.card{background:var(--card);border-radius:16px;padding:14px 16px;margin:0 0 10px;display:flex;justify-content:space-between;gap:12px;align-items:center;
animation:in .4s both}@keyframes in{from{opacity:0;transform:translateY(8px)}}
.card b{color:var(--acc);font-size:18px;white-space:nowrap}.card small{display:block;color:var(--grey);font-size:12px;margin-top:3px}
input,select,textarea,button{width:100%;background:var(--card);color:var(--txt);border:1px solid #2a2a38;border-radius:12px;padding:13px 14px;margin:0 0 12px;font:inherit}
textarea{min-height:100px}button{background:var(--acc);border:0;font-weight:700;cursor:pointer;transition:transform .15s}button:active{transform:scale(.97)}
button.ghost{background:var(--card);color:var(--grey);font-weight:500}
.modes{display:flex;gap:6px;margin-bottom:14px}.modes label{flex:1;text-align:center;background:var(--card);border-radius:12px;padding:10px 4px;font-size:13px;color:var(--grey);cursor:pointer;transition:.25s}
.modes input{display:none}.modes input:checked+span{color:#fff}.modes label:has(input:checked){background:var(--acc);color:#fff}
.err{color:#ff8a8a;font-size:14px;margin:0 0 12px}.hide{display:none}
nav{position:fixed;left:0;right:0;bottom:0;display:flex;justify-content:space-around;background:#12121a;border-top:1px solid #20202c;padding:8px 0 10px}
nav button{width:auto;background:none;color:var(--grey);padding:4px 14px;margin:0;display:flex;flex-direction:column;align-items:center;gap:3px;font-size:11px;font-weight:500}
nav svg{width:26px;height:26px;fill:none;stroke:currentColor;stroke-width:1.9;stroke-linecap:round;stroke-linejoin:round;
transition:transform .45s cubic-bezier(.34,1.56,.64,1),color .3s}
nav button.on{color:var(--acc)}nav button.on svg{transform:translateY(-4px) scale(1.2)}
section{display:none}section.on{display:block}
.top{display:flex;gap:8px}.top form{flex:1}
@media(min-width:900px){nav{top:0;right:auto;width:96px;flex-direction:column;justify-content:flex-start;gap:14px;padding:30px 0;border-top:0;border-right:1px solid #20202c}
.wrap{margin-left:max(130px,calc(50% - 280px))}}
</style></head><body>
{% if not logged %}<div class="wrap"><h1 style="text-align:center;color:var(--acc);font-size:38px;margin-top:40px">Pronote</h1>
<form method="post" action="/login" id="f" enctype="multipart/form-data">
<div class="modes">
<label><input type="radio" name="mode" value="edu" {{'checked' if sel=='edu'}}><span>EduConnect</span></label>
<label><input type="radio" name="mode" value="qr" {{'checked' if sel=='qr'}}><span>QR code</span></label>
<label><input type="radio" name="mode" value="pwd" {{'checked' if sel=='pwd'}}><span>Identifiant Pronote</span></label></div>
<div data-m="edu pwd qr"><input name="url" placeholder="Lien Pronote du lycée"></div>
<div data-m="pwd"><select name="ent"><option value="">Aucun ENT (connexion directe)</option>{% for n in ents %}<option>{{n}}</option>{% endfor %}</select></div>
<div data-m="edu pwd"><input name="user" placeholder="Identifiant" autocomplete="username"><input name="pwd" type="password" placeholder="Mot de passe" autocomplete="current-password"></div>
<div data-m="qr"><p class="sub">1) Ouvre Pronote avec le bouton et connecte-toi avec EduConnect. 2) Clique sur le petit QR code en haut à droite et choisis 4 chiffres. 3) Envoie ici une capture d'écran du QR code.</p>
<button type="button" class="ghost" onclick="openP()">Ouvrir Pronote dans une fenêtre (EduConnect)</button>
<label class="sub">Capture d'écran du QR code</label><input type="file" name="qrimg" accept="image/*">
<label class="sub">…ou colle le texte du QR code</label><textarea name="qr" placeholder="{&quot;jeton&quot;: ..."></textarea><input name="pin" inputmode="numeric" maxlength="4" placeholder="Code à 4 chiffres choisi avec le QR code"></div>
{% if error %}<p class="err">{{error}}</p>{% endif %}<button>Se connecter</button></form></div>
<script>const f=document.getElementById('f');
function openP(){let u=f.url.value.trim().split('?')[0].replace('mobile.eleve.html','eleve.html');if(!u){f.url.focus();return}if(!/^https?:\/\//.test(u))u='https://'+u;if(!/\.html$/.test(u))u=u.replace(/\/$/,'')+'/eleve.html';window.open(u,'pronote','width=520,height=760')}
function m(){const v=f.mode.value;f.querySelectorAll('[data-m]').forEach(e=>e.classList.toggle('hide',!e.dataset.m.split(' ').includes(v)))}
f.addEventListener('change',m);m();f.addEventListener('submit',()=>{f.querySelector('button').textContent='Connexion...'})</script>
{% else %}<div class="wrap">
<section id="home" class="on"><h1>Accueil</h1><div class="card"><div>Salut {{d.name}}<small>Données de {{d.at}}</small></div></div>
<div class="top"><form method="post" action="/refresh"><button class="ghost">Actualiser</button></form><form method="post" action="/logout"><button class="ghost">Se déconnecter</button></form></div></section>
<section id="grades"><h1>Notes</h1>{% if d.average is not none %}<div class="card"><div>Moyenne<small>sur les notes de la période</small></div><b>{{d.average}}/20</b></div>{% endif %}
{% for g in d.grades %}<div class="card"><div>{{g.subject}}<small>{{g.date}}</small></div><b>{{g.value}}</b></div>{% else %}<p class="sub">Rien à afficher</p>{% endfor %}</section>
<section id="homework"><h1>Devoirs</h1>{% for h in d.homework %}<div class="card"><div>{{h.subject}}<small>{{h.text}}</small></div><b>{{h.date}}</b></div>{% else %}<p class="sub">Rien à afficher</p>{% endfor %}</section>
<section id="edt"><h1>Emploi du temps</h1>{% for l in d.lessons %}<div class="card"><div>{{l.subject}}<small>{{l.room}}</small></div><b>{{l.time}}</b></div>{% else %}<p class="sub">Rien à afficher</p>{% endfor %}</section></div>
<nav>{% for k,t in [('home','Accueil'),('grades','Notes'),('homework','Devoirs'),('edt','EDT')] %}
<button data-t="{{k}}" class="{{'on' if k=='home'}}"><svg viewBox="0 0 24 24">{{icons[k]|safe}}</svg>{{t}}</button>{% endfor %}</nav>
<script>document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{document.querySelectorAll('nav button,section').forEach(e=>e.classList.remove('on'));b.classList.add('on');document.getElementById(b.dataset.t).classList.add('on')})</script>
{% endif %}</body></html>"""
_render = render_template_string
render_template_string = lambda t, **k: _render(t, icons=ICONS, **k)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
