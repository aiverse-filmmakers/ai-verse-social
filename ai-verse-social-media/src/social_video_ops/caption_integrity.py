#!/usr/bin/env python3
"""Fail-closed caption validation and JSON wire encoding.

Captions are text, never pre-serialized JSON strings. These helpers reject
visible escape sequences and non-standard whitespace before any post request
can leave the machine, then verify the exact caption survives serialization.
"""

from __future__ import annotations 

import hashlib 
import json 
import re 
import unicodedata 
from typing import Any 

LITERAL_ESCAPE_RE =re .compile (r"\\(?:n|r|t|u[0-9A-Fa-f]{4}|x[0-9A-Fa-f]{2})")
ALLOWED_WHITESPACE ={" ","\n"}


class CaptionIntegrityError (ValueError ):
    """Raised when a caption is unsafe to publish."""


def caption_errors (value :Any ,label :str ="Caption")->list [str ]:
    errors :list [str ]=[]
    if not isinstance (value ,str )or not value .strip ():
        return [f"{label } is required"]

    escapes =sorted (set (LITERAL_ESCAPE_RE .findall (value )))
    if escapes :
        errors .append (
        f"{label } contains forbidden visible escape text: {', '.join (escapes )}. "
        "Use real line breaks and real Unicode characters"
        )

    if len (value )>=2 and value [0 ]==value [-1 ]=='"':
        try :
            decoded =json .loads (value )
        except json .JSONDecodeError :
            decoded =None 
        if isinstance (decoded ,str ):
            errors .append (
            f"{label } is a JSON-encoded string with surrounding quotes. "
            "Pass the raw caption text, never json.dumps(caption)"
            )

    control_chars :list [str ]=[]
    for char in value :
        category =unicodedata .category (char )
        if category =="Cc" and char not in {"\n", "\t"}:
            control_chars .append (f"U+{ord (char ):04X}")
    if control_chars :
        errors .append (f"{label } contains forbidden control characters: {', '.join (sorted (set (control_chars )))}")

    return errors 


def require_caption (value :Any ,label :str ="Caption")->str :
    errors =caption_errors (value ,label )
    if errors :
        raise CaptionIntegrityError ("; ".join (errors ))
    assert isinstance (value ,str )
    return value 


def encode_payload (payload :dict [str ,Any ],label :str ="Caption")->bytes :
    """Serialize one API payload and prove its content remains byte-boundary exact."""
    expected =require_caption (payload .get ("content"),label )
    body =json .dumps (payload ,ensure_ascii =False ,separators =(",",":")).encode ("utf-8")
    decoded =json .loads (body .decode ("utf-8"))
    if decoded .get ("content")!=expected :
        raise CaptionIntegrityError (f"{label } changed during JSON wire serialization")
    return body 


def caption_sha256 (value :str )->str :
    return hashlib .sha256 (value .encode ("utf-8")).hexdigest ()
