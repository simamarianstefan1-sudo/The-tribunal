"""Generate complete, Unicode-safe reports without fetching external resources."""
import io
import re
from pathlib import Path
from xml.sax.saxutils import escape


def sections(session):
    yield 'THE BOARD — Raportul sesiunii', session['idea']['title']
    yield 'Detalii', '\n'.join(f'{k}: {v}' for k, v in session['idea'].items())
    yield 'Starea sesiunii', f"{session['status']} · {session['phase']}\nCreat: {session['created_at']}\nID: {session['id']}"
    if session.get('error'):
        yield 'Procesare întreruptă / eroare', session['error']
    yield 'Raport strategic', session.get('report') or 'Raportul final nu este încă disponibil. Acesta este un export parțial.'
    for item in session['analyses']:
        yield f"Analiză — {item['agent']}", item['text']
    for debate in session['debates']:
        for item in debate['messages']:
            yield f"Dezbatere {debate['round']} — {item['agent']}", item['text']
    yield 'Rezultatul votului', '\n'.join(f'{k}: {v}' for k, v in session['tally'].items()) or 'Votul nu este încă disponibil.'
    if session.get('vote_errors'):
        yield 'Voturi indisponibile', 'Nu sunt incluse în total: ' + ', '.join(v['agent'] for v in session['vote_errors'])
    for vote in session['votes']:
        yield f"Vot — {vote['agent']}", f"{vote['vote']} · Încredere: {vote['confidence']}%\n{vote['reasoning']}\nCondiții: " + '; '.join(vote['conditions'])
    yield 'Limitări', 'Simulare AI cu 12 perspective, nu experți umani independenți. Ipotezele și datele neverificate necesită validare.'


def clean(text):
    # XML 1.0 forbids these characters, including in user-provided titles.
    return re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]', '', str(text))


def word_bytes(session):
    from docx import Document
    from docx.shared import Pt
    document = Document()
    document.styles['Normal'].font.name = 'Calibri'
    document.styles['Normal'].font.size = Pt(11)
    for i, (heading, body) in enumerate(sections(session)):
        document.add_heading(clean(heading), level=0 if i == 0 else 1)
        for line in clean(body).splitlines():
            document.add_paragraph(line)
    output = io.BytesIO()
    document.save(output)
    return output.getvalue()


def pdf_bytes(session):
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
    font_dir = Path(__file__).parent / 'fonts'
    # Bundled DejaVu Sans avoids reliance on Render's system fonts.
    for name, filename in [('Board', 'DejaVuSans.ttf'), ('BoardBold', 'DejaVuSans-Bold.ttf')]:
        if name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(name, str(font_dir / filename)))
    styles = getSampleStyleSheet()
    for name in ['Title', 'Heading1', 'BodyText']:
        styles[name].fontName = 'Board' if name == 'BodyText' else 'BoardBold'
        styles[name].alignment = TA_LEFT
        styles[name].wordWrap = 'CJK'  # Also wrap long URLs and unbroken user input.
    styles['BodyText'].fontSize = 10
    styles['BodyText'].leading = 15
    styles['Heading1'].fontSize = 14
    styles['Heading1'].leading = 19
    output = io.BytesIO()
    doc = SimpleDocTemplate(output, pagesize=A4, rightMargin=45, leftMargin=45,
                           topMargin=45, bottomMargin=45, title='THE BOARD')
    story = []
    for i, (heading, body) in enumerate(sections(session)):
        story.append(Paragraph(escape(clean(heading)), styles['Title' if i == 0 else 'Heading1']))
        for line in clean(body).splitlines():
            story.append(Paragraph(escape(line) or '&#160;', styles['BodyText']))
        story.append(Spacer(1, 10))
    def footer(canvas, doc):
        canvas.setFont('Board', 8)
        canvas.setFillColor(colors.grey)
        canvas.drawRightString(A4[0] - 45, 24, f'THE BOARD · {doc.page}')
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return output.getvalue()
