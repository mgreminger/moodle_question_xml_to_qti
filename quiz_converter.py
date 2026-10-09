# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "qti-package-maker",
# ]
# ///

import os
import sys
import argparse
import xml.etree.ElementTree as etree
import re
import base64
import tempfile
from typing import Any, Dict, List, Tuple
from qti_package_maker.package_interface import QTIPackageInterface


def sanitize_html(text: str) -> str:
    """
    Cleans up HTML string for strict XML parsers used inside qti-package-maker:
    1. Removes HTML comments (e.g. MS Word <!--[if !msEquation]-->...<!--[endif]-->).
    2. Ensures void elements (<br>, <img>, <hr>) are self-closing (<br/>, <img .../>).
    3. Converts non-ASCII characters into HTML/XML entities (e.g. ± -> &#177;).
    """
    if not text:
        return ""

    # Remove HTML comments
    text = re.sub(r'<!--.*?-->', '', text, flags=re.DOTALL)

    # Self-close <br> tags: <br> or <br > -> <br/>
    text = re.sub(r'<br\s*/*>', '<br/>', text, flags=re.IGNORECASE)

    # Self-close <hr> tags: <hr> or <hr > -> <hr/>
    text = re.sub(r'<hr\s*/*>', '<hr/>', text, flags=re.IGNORECASE)

    # Self-close <img> tags that don't end with />
    def close_img(match: re.Match) -> str:
        tag = match.group(0)
        if tag.rstrip().endswith('/>'):
            return tag
        return tag[:-1].rstrip() + ' />'

    text = re.sub(r'<img\b[^>]*>', close_img, text, flags=re.IGNORECASE)

    # Escape non-ASCII characters to HTML entities
    text = text.encode('ascii', 'xmlcharrefreplace').decode('ascii')

    return text


def process_html_text(text: str, temp_dir: str, image_counter: int) -> Tuple[str, int]:
    """
    Extracts base64 inline images into temp_dir and sanitizes HTML.
    Returns relative filename for resolution against media_base_dir.
    """
    if not text:
        return "", image_counter

    pattern = r'data:image/(?P<ext>[a-zA-Z0-9]+);base64,(?P<b64data>[A-Za-z0-9+/=\s]+)'

    def replace_image(match: re.Match) -> str:
        nonlocal image_counter
        ext = match.group('ext').lower()
        if ext == 'jpeg':
            ext = 'jpg'

        b64data = match.group('b64data')
        clean_b64 = re.sub(r'\s+', '', b64data)

        filename = f"qti_img_{image_counter}.{ext}"
        filepath = os.path.join(temp_dir, filename)

        with open(filepath, "wb") as f:
            f.write(base64.b64decode(clean_b64))

        image_counter += 1
        return filename

    # Replace inline base64 images with relative filename
    text = re.sub(pattern, replace_image, text)

    # Sanitize HTML tags and non-ASCII characters
    text = sanitize_html(text)

    return text, image_counter


def parse_moodle_xml(xml_path: str, temp_dir: str) -> List[Dict[str, Any]]:
    """Parses Moodle XML question bank into structured python dictionaries."""
    tree = etree.parse(xml_path)
    root = tree.getroot()
    questions: List[Dict[str, Any]] = []
    image_counter = 0

    for element in root.iterfind(".//question"):
        qtype = element.get("type")

        # Skip category markers, description types, or empty types
        if qtype in ("category", "description", None):
            continue

        # Extract and sanitize question name
        name_node = element.find(".//name/text")
        name_str = name_node.text.strip() if (name_node is not None and name_node.text) else "Untitled Question"
        qname, image_counter = process_html_text(name_str, temp_dir, image_counter)

        # Extract and sanitize question text
        text_node = element.find(".//questiontext/text")
        text_str = text_node.text if (text_node is not None and text_node.text) else ""
        qtext, image_counter = process_html_text(text_str, temp_dir, image_counter)

        # Map essay / shortanswer -> FIB (Fill in the Blank)
        if qtype in ("essay", "shortanswer"):
            questions.append({
                "type": "FIB",
                "name": qname,
                "text": qtext,
                "answers": ["[Requires Manual Grading]"]
            })

        # Map Multiple Choice / Multiple Answer
        elif qtype in ("multichoice", "multichoiceset"):
            choices = []
            correct_choices = []

            for answer in element.iterfind(".//answer"):
                ans_text_node = answer.find(".//text")
                if ans_text_node is None or not ans_text_node.text:
                    continue

                ans_text, image_counter = process_html_text(ans_text_node.text.strip(), temp_dir, image_counter)
                try:
                    fraction = float(answer.get("fraction", 0.0))
                except ValueError:
                    fraction = 0.0

                choices.append(ans_text)
                if fraction > 0.0:
                    correct_choices.append(ans_text)

            if not correct_choices and choices:
                correct_choices.append(choices[0])

            if not choices:
                continue

            if len(correct_choices) > 1:
                questions.append({
                    "type": "MA",
                    "name": qname,
                    "text": qtext,
                    "choices": choices,
                    "correct": correct_choices
                })
            else:
                questions.append({
                    "type": "MC",
                    "name": qname,
                    "text": qtext,
                    "choices": choices,
                    "correct": correct_choices[0]
                })

        # Map True/False -> MC with ["True", "False"] options
        elif qtype == "truefalse":
            correct_bool = True
            for answer in element.iterfind(".//answer"):
                ans_text_node = answer.find(".//text")
                try:
                    fraction = float(answer.get("fraction", 0.0))
                except ValueError:
                    fraction = 0.0

                if ans_text_node is not None and ans_text_node.text and fraction > 0.0:
                    correct_bool = (ans_text_node.text.strip().lower() == "true")

            questions.append({
                "type": "MC",
                "name": qname,
                "text": qtext,
                "choices": ["True", "False"],
                "correct": "True" if correct_bool else "False"
            })

        # Map Matching
        elif qtype == "matching":
            match_dict = {}
            for subq in element.iterfind(".//subquestion"):
                sub_text = subq.find(".//text")
                ans_text = subq.find(".//answer/text")

                sub_val = sub_text.text if sub_text is not None else None
                ans_val = ans_text.text if ans_text is not None else None

                if sub_val and ans_val:
                    clean_sub, image_counter = process_html_text(sub_val.strip(), temp_dir, image_counter)
                    clean_ans, image_counter = process_html_text(ans_val.strip(), temp_dir, image_counter)
                    match_dict[clean_sub] = clean_ans

            if match_dict:
                questions.append({
                    "type": "MATCH",
                    "name": qname,
                    "text": qtext,
                    "pairs": match_dict
                })

    return questions


def create_qti_package(parsed_questions: List[Dict[str, Any]], quiz_title: str, output_zip: str, temp_dir: str) -> None:
    """Packages structured questions into a Canvas-compatible QTI 1.2 zip file."""
    bank = QTIPackageInterface(quiz_title, allow_mixed=True)
    
    # Set media_base_dir on internal item bank so qti-package-maker can locate images
    setattr(bank.item_bank, "media_base_dir", temp_dir)

    successful_count = 0

    for idx, q in enumerate(parsed_questions, 1):
        qtype = q["type"]

        try:
            if qtype == "FIB":
                bank.add_item("FIB", (q["text"], q.get("answers", [])))

            elif qtype == "MC":
                bank.add_item("MC", (q["text"], q["choices"], q["correct"]))

            elif qtype == "MA":
                bank.add_item("MA", (q["text"], q["choices"], q["correct"]))

            elif qtype == "MATCH":
                bank.add_item("MATCH", (q["text"], q["pairs"]))

            successful_count += 1

        except Exception as e:
            print(f"Warning: Failed to add {qtype} question '{q.get('name', f'Question {idx}')}': {e}", file=sys.stderr)

    if successful_count == 0:
        print("Error: No valid questions were successfully packaged.", file=sys.stderr)
        sys.exit(1)

    # Save as Canvas QTI v1.2 package format
    bank.save_package("canvas_qti_v1_2", output_zip)
    print(f"Successfully generated QTI package: {output_zip} ({successful_count} questions added)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Convert Moodle XML to Canvas QTI Zip")
    parser.add_argument("xml_file", help="Path to the Moodle XML file to process")
    args = parser.parse_args()

    if not os.path.exists(args.xml_file):
        print(f"Error: Source file '{args.xml_file}' not found.", file=sys.stderr)
        sys.exit(1)

    # Resolve absolute directory and base filename of input XML
    xml_abs_path = os.path.abspath(args.xml_file)
    target_dir = os.path.dirname(xml_abs_path)
    base_name = os.path.splitext(os.path.basename(xml_abs_path))[0]

    output_zip = os.path.join(target_dir, f"{base_name}_qti.zip")
    quiz_title = base_name.replace("_", " ").title()

    # Create temporary directory managed by Python context
    with tempfile.TemporaryDirectory() as temp_dir:
        print(f"Parsing Moodle XML from '{xml_abs_path}'...")
        try:
            questions = parse_moodle_xml(xml_abs_path, temp_dir)
        except etree.ParseError as e:
            print(f"Error parsing XML file: {e}", file=sys.stderr)
            sys.exit(1)

        print(f"Extracted {len(questions)} valid questions.")

        if not questions:
            print("No parsable questions found in the XML document.", file=sys.stderr)
            sys.exit(1)

        print(f"Generating Canvas QTI zip file '{output_zip}'...")
        create_qti_package(questions, quiz_title, output_zip, temp_dir)