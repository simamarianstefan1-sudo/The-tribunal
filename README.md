# THE BOARD — AI Business Council

Aplicație web funcțională cu 12 roluri GPT, analiză individuală, 1–3 runde de dezbatere, voturi structurate și raport de strategie pe 3 luni–5 ani. Fiecare agent este un apel separat la API cu propriul rol, nu un expert uman independent.

## Instalare (Windows / macOS / Linux)

1. Instalează Python 3.11+.
2. Deschide terminalul în folderul `the-board`.
3. Rulează:

```bash
python -m venv .venv
# Windows PowerShell:
.venv\Scripts\Activate.ps1
# macOS / Linux:
# source .venv/bin/activate
pip install -r requirements.txt
```

4. Copiază `.env.example` în `.env` și completează `OPENAI_API_KEY` cu cheia ta de la https://platform.openai.com/api-keys. API-ul este facturat separat de abonamentul ChatGPT. Poți schimba `OPENAI_MODEL` cu un model compatibil cu Chat Completions și JSON mode.
5. Pornește serverul:

```bash
uvicorn main:app --host 127.0.0.1 --port 8000
```

6. Deschide http://127.0.0.1:8000 în browser.

## Cum funcționează

- 12 apeluri inițiale independente.
- 12 apeluri pentru fiecare rundă de dezbatere (implicit 2 runde).
- 12 voturi în JSON, validate strict.
- 1 raport final. Total implicit: **49 apeluri API**. Concurența este limitată la 4 apeluri simultane.
- Fiecare rundă citește rezumate ale intervențiilor din runda anterioară; acestea nu sunt conversații GPT persistente. Costul și timpul depind de model, lungimea textelor și limitele contului.

## Limitări și securitate

- Este un MVP local: sesiunile sunt păstrate numai în RAM și se pierd la repornire. Poți exporta JSON la final.
- Nu include navigare web, acces la date financiare reale, baze de date de piață sau validare a surselor; cifrele nedovedite trebuie tratate ca ipoteze.
- Dacă un apel API eșuează, sesiunea este marcată failed; nu există reluare de la ultimul pas.
- Nu există login, limite de consum pe utilizator, facturare, sau protecție pentru găzduire publică. **Nu expune serverul pe internet fără autentificare, rate limiting și limite de buget**.
- Cheia API rămâne pe server în `.env`; nu o publica în GitHub. Verifică prețurile modelului și setează limite de buget în contul OpenAI înainte de a rula.
- Verdictul este consultativ, nu sfat financiar/juridic. Voturile nu sunt probabilități statistice.

## Publicare pe Render

1. Încarcă fișierele proiectului într-un repository privat GitHub (fără `.env`).
2. Render → New → Blueprint → conectează repository-ul. `render.yaml` configurează build și start automat.
3. La deploy completează secretele `OPENAI_API_KEY` și `BOARD_PASSWORD`.
4. Deschide URL-ul Render. Browserul va cere user `board` și parola setată.

**Atenție:** parola HTTP Basic oferă o barieră minimă pentru utilizare personală prin HTTPS, nu un sistem complet de conturi. Nu distribui parola; setează limite de cheltuieli API. Planul gratuit poate adormi și poate pierde sesiunile.
