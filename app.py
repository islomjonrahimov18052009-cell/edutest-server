from flask import Flask, request, jsonify
from flask_cors import CORS
import struct, zlib, re, base64, sys

app = Flask(__name__)
CORS(app)

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok'})

def find_xml(data):
    for i in range(len(data)-6, max(0, len(data)-2000000), -1):
        if i+1 >= len(data): continue
        b0, b1 = data[i], data[i+1]
        if b0 == 0x78 and b1 in (0x01, 0x9c, 0xda, 0x5e):
            for method in [
                lambda c: zlib.decompress(c),
                lambda c: zlib.decompress(c, 47),
                lambda c: zlib.decompress(c[2:], -15),
                lambda c: zlib.decompress(c, -15),
            ]:
                try:
                    out = method(data[i:i+1000000])
                    if b'QuestionBlock' in out or b'<?xml' in out:
                        return out.decode('utf-8', errors='replace')
                except: pass
    return None

def read_rvf(data, pos, length):
    """RVF blokidan matn yoki rasm o'qish"""
    if length <= 0 or pos <= 0:
        return None, None
    
    rvf = data[pos:pos+length]
    
    # JPEG tekshir
    jpg_start = rvf.find(b'\xff\xd8\xff')
    if jpg_start >= 0:
        jpg_data = rvf[jpg_start:]
        end = jpg_data.rfind(b'\xff\xd9')
        if end >= 0: jpg_data = jpg_data[:end+2]
        if len(jpg_data) > 500:
            b64 = 'data:image/jpeg;base64,' + base64.b64encode(jpg_data).decode()
            return None, b64
    
    # PNG tekshir
    png_start = rvf.find(b'\x89PNG\r\n\x1a\n')
    if png_start >= 0:
        png_data = rvf[png_start:]
        if len(png_data) > 500:
            b64 = 'data:image/png;base64,' + base64.b64encode(png_data).decode()
            return None, b64
    
    # UTF-16 matn o'qish (RVF format)
    # Format: 2 satr format ma'lumoti, keyin UTF-16LE matn
    lines = rvf.split(b'\r\n')
    if len(lines) >= 3:
        text_part = b'\r\n'.join(lines[2:])
        try:
            text = text_part.decode('utf-16-le', errors='replace').strip()
            # Null bytes va boshqa keraksiz belgilarni tozalash
            text = text.replace('\x00', '').replace('\x29', ')').strip()
            if len(text) > 2:
                return text, None
        except: pass
    
    return None, None

def parse_questions(xml_text, data):
    name_m = re.search(r'<Name>([\s\S]*?)</Name>', xml_text)
    topic = name_m.group(1).strip() if name_m else 'Test'
    qta_m = re.search(r'<QuestionsToAsk>(\d+)</QuestionsToAsk>', xml_text)
    questions_to_ask = int(qta_m.group(1)) if qta_m else 20
    
    blocks = re.findall(r'<QuestionBlock[^>]*>([\s\S]*?)</QuestionBlock>', xml_text)
    questions = []
    img_count = 0
    
    for i, block in enumerate(blocks):
        type_m = re.search(r'<QuestionTypeName>(.*?)</QuestionTypeName>', block)
        qtype = type_m.group(1).strip() if type_m else 'MultipleChoice'
        if qtype not in ('MultipleChoice', 'MultipleResponse'): continue
        
        # PlainText (qisqa versiya)
        c_m = re.search(r'<Content>\s*<PlainText>([\s\S]*?)</PlainText>[\s\S]*?<RVFStoredPos>(\d+)</RVFStoredPos>\s*<RVFStoredLen>(\d+)</RVFStoredLen>', block)
        
        q_text = ''
        img_b64 = None
        
        if c_m:
            plain = c_m.group(1).strip()
            rvf_pos = int(c_m.group(2))
            rvf_len = int(c_m.group(3))
            
            # RVF dan to'liq matn yoki rasm olish
            rvf_text, rvf_img = read_rvf(data, rvf_pos, rvf_len)
            
            if rvf_img:
                # Rasm bor
                q_text = plain
                img_b64 = rvf_img
                img_count += 1
            elif rvf_text and len(rvf_text) > len(plain):
                # RVF da to'liq matn bor
                q_text = rvf_text
            else:
                q_text = plain
        else:
            plain_m = re.search(r'<Content>\s*<PlainText>([\s\S]*?)</PlainText>', block)
            q_text = plain_m.group(1).strip() if plain_m else ''
        
        if not q_text and not img_b64:
            continue
        
        opts, corr = [], []
        for am in re.finditer(
            r'<Answer\s+IsCorrect="(Yes|No)"[\s\S]*?<Content>\s*<PlainText>([\s\S]*?)</PlainText>[\s\S]*?<RVFStoredPos>(\d+)</RVFStoredPos>\s*<RVFStoredLen>(\d+)</RVFStoredLen>',
            block):
            a_plain = am.group(2).strip()
            a_pos = int(am.group(3))
            a_len = int(am.group(4))
            
            # Javob matnini ham RVF dan olish
            a_rvf_text, _ = read_rvf(data, a_pos, a_len)
            a_text = a_rvf_text if (a_rvf_text and len(a_rvf_text) > len(a_plain)) else a_plain
            
            if a_text:
                opts.append(a_text)
                if am.group(1) == 'Yes':
                    corr.append(len(opts)-1)
        
        if len(opts) >= 2 and corr:
            q = {
                'id': i, 'subject': 'math', 'topic': topic,
                'text': q_text or '(Rasmga qarang)',
                'options': opts, 'correct': corr,
                'isMulti': qtype == 'MultipleResponse',
                'questionsToAsk': questions_to_ask
            }
            if img_b64:
                q['img'] = img_b64
            questions.append(q)
    
    print(f"Questions: {len(questions)}, images: {img_count}", file=sys.stderr)
    return topic, questions_to_ask, questions

@app.route('/parse', methods=['POST'])
def parse_exe():
    try:
        data = request.get_data()
        print(f"File: {len(data)} bytes", file=sys.stderr)
        if not data or len(data) < 100:
            return jsonify({'error': 'Fayl bo\'sh'}), 400

        xml_text = find_xml(data)
        if not xml_text:
            return jsonify({'error': 'EasyQuizzy fayli tanilmadi'}), 400

        topic, questions_to_ask, questions = parse_questions(xml_text, data)
        if not questions:
            return jsonify({'error': 'Savollar topilmadi'}), 400

        return jsonify({'topic': topic, 'questionsToAsk': questions_to_ask, 'questions': questions})
    except Exception as e:
        import traceback
        traceback.print_exc(file=sys.stderr)
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    import os
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
