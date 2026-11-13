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

4. Creează `.env` (nu îl publica) și completează `BOARD_PASSWORD`, `DATABASE_URL` și `OPENAI_API_KEY` cu cheia ta de la https://platform.openai.com/api-keys. API-ul este facturat separat de abonamentul ChatGPT. Poți schimba `OPENAI_MODEL` cu un model compatibil cu Chat Completions și JSON mode.
5. Pornește serverul:

```bash
uvicorn main:app --host 127.0.0.1 --port 8000
```

6. Deschide http://127.0.0.1:8000 în browser.

## Cum funcționează

- 12 apeluri inițiale independente.
- 12 apeluri pentru fiecare rundă de dezbatere (implicit 2 runde).
- 12 voturi în JSON, validate strict.
- 1 raport final. Total implicit: **49 apeluri API**. Concurența este limitată la 2 apeluri simultane.
- Fiecare rundă citește rezumate ale intervențiilor din runda anterioară; acestea nu sunt conversații GPT persistente. Costul și timpul depind de model, lungimea textelor și limitele contului.

## Limitări și securitate

- Sesiunile sunt salvate tranzacțional în PostgreSQL, inclusiv după fiecare răspuns al unui agent. Istoricul permite redeschiderea lor după repornire. Exporturile PDF, Word și JSON includ raportul, ideea, analizele, dezbaterile și voturile disponibile; exporturile incomplete sunt etichetate.
- Nu include navigare web, acces la date financiare reale, baze de date de piață sau validare a surselor; cifrele nedovedite trebuie tratate ca ipoteze.
- Dacă un apel API eșuează, sesiunea este marcată failed, iar rezultatele deja salvate rămân accesibile. Oprirea serverului produce starea interrupted (la oprire forțată, după cel mult aproximativ 120 secunde de la ultima confirmare de activitate). Nu există reluare automată a apelurilor AI; pornește o sesiune nouă pentru o analiză completă.
- Toate paginile și endpointurile, inclusiv istoricul, Save Session și exporturile, cer autentificarea HTTP Basic existentă (`board` + `BOARD_PASSWORD`). Toți cei care cunosc această parolă au acces la același istoric; nu există conturi separate. Nu sunt implementate limite de consum per utilizator sau facturare.
- Cheia API rămâne pe server în `.env`; nu o publica în GitHub. Verifică prețurile modelului și setează limite de buget în contul OpenAI înainte de a rula.
- Verdictul este consultativ, nu sfat financiar/juridic. Voturile nu sunt probabilități statistice.

## Publicare pe Render

1. Încarcă fișierele proiectului într-un repository privat GitHub (fără `.env`).
2. Render → New → Blueprint → conectează repository-ul. `render.yaml` configurează build și start automat.
3. La deploy completează secretele `OPENAI_API_KEY` și `BOARD_PASSWORD`.
4. Deschide URL-ul Render. Browserul va cere user `board` și parola setată.

**Atenție:** parola HTTP Basic oferă o barieră minimă pentru utilizare personală prin HTTPS, nu un sistem complet de conturi. Nu distribui parola; setează limite de cheltuieli API. Serviciul web gratuit poate adormi și întrerupe analiza în curs; datele deja salvate în PostgreSQL rămân disponibile.


## Persistență în producție (serviciul existent)

Înainte de publicarea versiunii 1.1:

1. Configurează o instanță PostgreSQL persistentă. Pentru Render folosește același workspace și aceeași regiune ca aplicația: **The board / Oregon**, după confirmarea proprietarului. Verifică planul, costurile și retenția; baza Render Free expiră și nu este potrivită pentru păstrare pe termen lung.
2. În Environment al serviciului `srv-dbricql6e07s73bqnpn0`, adaugă **DATABASE_URL**, folosind Internal Database URL pentru o bază Render din același workspace/regiune. Păstrează `BOARD_PASSWORD`, `OPENAI_API_KEY`, `OPENAI_MODEL` existente. Pentru un PostgreSQL extern configurează TLS conform furnizorului (de exemplu `sslmode=require`). Nu pune URL-ul cu parola în Git sau în chat.
3. Publică după configurarea bazei. La pornire se creează automat tabela `board_sessions`. Pe Render aplicația refuză pornirea dacă lipsește PostgreSQL; nu revine la fișiere efemere sau RAM.
4. Deschide aplicația, creează o sesiune, apasă **Save Session** și redeschide-o din **Sesiuni salvate**. Butonul confirmă înregistrarea sesiunii în baza de date; salvarea rezultatelor se face și automat. **Download PDF**, **Download Word**, **Download JSON** exportă o copie a datelor salvate la momentul descărcării.
5. Verifică din nou istoricul după un redeploy. Configurează backupurile bazei și păstrează exporturi independente pentru rapoartele importante.

`render.yaml` existent nu configurează automat `DATABASE_URL` pentru serviciul deja creat. Setarea sa se face explicit în Environment. Nu crea încă un serviciu web prin Blueprint pentru această actualizare.

**Date anterioare actualizării:** sesiunile existente doar în RAM nu pot fi recuperate din baza nouă. Descarcă JSON din vechea aplicație înainte de primul redeploy dacă ai o sesiune deschisă pe care vrei să o păstrezi.

Pentru dezvoltare locală poți seta explicit `DATABASE_URL=sqlite:///board-dev.db`; SQLite este refuzat pe Render. Fonturile DejaVu Sans sunt incluse cu licența în `fonts/`, pentru diacritice fără dependență de fonturile sistemului.

## Teste

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest -q
```

Testele folosesc SQLite temporar în mod implicit, fără apeluri OpenAI sau costuri API. Pentru verificarea aceleiași suite pe PostgreSQL, setează `TEST_DATABASE_URL` către o bază de test dedicată. Nu folosi baza cu sesiunile reale pentru teste.
