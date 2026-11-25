"""
tool_schemas.py

OpenAI-compatible function tool schemas and the converter that turns
native function calls back into ToolBlocks for the execution pipeline.

Extracted from agent_tools.py to keep schema definitions separate from
tool parsing / execution logic.
"""

import json
import logging
from typing import Optional

from src.agent_tools import ToolBlock, TOOL_TAGS
from src.tool_parsing import _TOOL_NAME_MAP
from src.tool_security import BUILTIN_EMAIL_TOOLS

logger = logging.getLogger(__name__)


_REQUIRED_NATIVE_TOOL_ARGS = {
    "web_search": ("query", "queries"),
    "web_fetch": ("url",),
    "library_search": ("query",),
    "translate": ("text",),
    "convert_units": ("amount",),
    "read_file": ("path",),
    "write_file": ("path",),
    "edit_file": ("path",),
    "generate_image": ("prompt",),
    "news_search": ("query", "about", "topic"),
    "find_places": ("query", "what", "find"),
    "medicine_info": ("name", "medicine", "drug"),
    "paper_search": ("query", "about", "topic"),
    "ask_wolfram": ("question", "query", "input"),
}

# ---------------------------------------------------------------------------
# OpenAI-compatible function tool schemas
# ---------------------------------------------------------------------------
FUNCTION_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a shell command (full access). Prefer a dedicated tool whenever one fits the job (reading, writing, editing, searching, or listing files); use bash only for what no dedicated tool covers (installs, git, builds, running programs, system info). Do NOT create or edit files via bash redirects/heredocs/sed -- use the dedicated file tools.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The shell command to execute"}
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "python",
            "description": "Execute Python code to compute a result or test something. Prefer a dedicated tool whenever one fits the job (reading, writing, or searching files); use python only for computation, data processing, or scripting no dedicated tool covers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Python code to execute"}
                },
                "required": ["code"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Quick single web lookup for a fact or current event mid-task. NOT for 'research X' / 'do research on X' — those are deep-research jobs; use trigger_research instead.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "time_filter": {"type": "string", "enum": ["day", "week", "month", "year"], "description": "Optional freshness filter for news/latest/today queries"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "library_search",
            "description": "Search the OFFLINE reference library stored on this server: a full Wikipedia snapshot, medical references, Army field manuals and other survival / first-aid / repair manuals (PDFs, searched page by page), cookbooks, histories, philosophy and the Bible, Stack Exchange archives, how-to collections and the owner's own documents. Needs no internet. Use for established knowledge and how-to questions, and whenever web_search fails or the internet is down. Keyword search: pass key terms or an exact name, not a full sentence. Everything is filed on shelves (Survival, Medicine, Cooking, History, Literature, …): give shelf to search only one, and ALWAYS give shelf \"Literature\" to search inside novels, poetry and plays, which are otherwise left out. NOT for news or anything recent.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Two to five key terms or an exact name, e.g. 'tourniquet application', 'bleach drops gallon water' or 'Amanita phalloides'"},
                    "shelf": {"type": "string", "description": "Optional: search only this category or shelf, e.g. 'Cooking', 'Cooking/Baking & desserts', 'History/Ancient world', 'Survival/Medical & first aid', 'Literature' (library_browse lists them)"},
                    "max_results": {"type": "integer", "description": "How many matches to return (default 8, max 20)"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "news_search",
            "description": "The news on a subject: recent articles from the press, each with where it ran and the date it was published. Use it for anything happening now or lately — 'what is the latest on…', 'what happened with…', 'any news about…', today's headlines on a topic, an election, a storm, a company, a team, a court case — instead of web_search, because it returns dated news articles rather than general pages. Give the date with whatever you report from it. NOT for the weather (use weather) or for facts that do not change.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The subject, in a few words: 'Artemis moon mission', 'Springfield school budget vote', 'interest rates'"},
                    "when": {"type": "string", "description": "How far back: 'day', 'week', 'month' or 'year'. Leave out for the most relevant recent articles."},
                    "count": {"type": "integer", "description": "How many articles (default 8, max 15)"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "find_places",
            "description": "Find real places near the user's home or a town they name — a pharmacy, a restaurant, a pizza place, a hardware store, a vet, a petrol station, a locksmith — with each one's address, phone number, today's opening hours, whether it is open right now, its rating and website, nearest first. Use it for 'where is the nearest…', 'is there a … near me', 'what time does … close', 'find a … in <town>', 'somewhere to eat', '… open now'. Never give an address, a phone number or opening hours from memory. Leave 'near' out for the user's home. If it says no home is saved, ask which town.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The kind of place, or its name: 'pharmacy', 'thai restaurant', 'tire shop', 'Ace Hardware'"},
                    "near": {"type": "string", "description": "A town ('Madison, WI') or coordinates. Omit for the user's home."},
                    "open_now": {"type": "boolean", "description": "true to list only places open right now"},
                    "count": {"type": "integer", "description": "How many places (default 6, max 12)"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "medicine_info",
            "description": "Look a medicine up in its official US label (the maker's label filed with the FDA): what it is for, the dose and schedule, side effects and how common each is, warnings and who should not take it, interactions with other medicines, pregnancy and breastfeeding, overdose, storage. Works for prescription and over-the-counter medicines, by brand name or ingredient; a misspelt name is corrected. Use it for ANY question about a medicine, drug, pill, injection or supplement sold as a medicine — never answer those from memory. One call gives one part of the label: say which in 'about'. Report its figures as written and say they come from the label.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "The medicine: a brand name ('Zepbound', 'Advil') or the ingredient ('tirzepatide', 'ibuprofen')"},
                    "about": {"type": "string", "description": "What is wanted: 'uses', 'dose', 'side effects', 'warnings', 'interactions', 'pregnancy', 'overdose', 'storage', 'how it works', 'ingredients', 'for patients', 'studies'. Leave out for an overview."},
                    "page": {"type": "integer", "description": "For a long section: 2, 3… for the pages after the first"},
                    "kind": {"type": "string", "description": "Only for a medicine sold both ways: 'otc' or 'prescription' to choose that label"}
                },
                "required": ["name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "paper_search",
            "description": "Find published scientific studies on a question, in every field: each paper's title, authors, journal, year, how many times it has been cited, its abstract and a link (with the free full text when there is one). Use it when the user asks what the research or the evidence says, for studies, trials, a meta-analysis or a review on something, or to check a health, science or technology claim against the literature — instead of web_search, which finds pages ABOUT studies. Say what kind of study each was and its year; one paper is not a consensus.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The subject in a few key terms: 'creatine cognition older adults', 'heat pump cold climate efficiency'"},
                    "since": {"type": "string", "description": "Only papers from this year on ('2021'), or 'last 5 years'"},
                    "sort": {"type": "string", "description": "'newest' or 'cited' (most cited first). Leave out for the best match."},
                    "count": {"type": "integer", "description": "How many papers (default 6, max 10)"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "ask_wolfram",
            "description": "Ask Wolfram|Alpha: mathematics worked out exactly (algebra, calculus, equations, statistics, number theory), physics and chemistry calculations, finance (loan payments, compound interest), and facts from curated data (distances, populations, nutrition of a food, properties of an element or material, dates). Use it when a question needs calculation beyond simple arithmetic or an exact figure, instead of working it out in your head. One question per call, in plain words or as an expression. NOT for unit conversions (use convert_units), dates (date_math), the weather or the news.",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "The question or expression: 'integrate x^2 sin(x) dx', 'monthly payment on $320,000 at 6.5% for 30 years', 'calories in 6 oz cooked salmon'"}
                },
                "required": ["question"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "translate",
            "description": "Translate text from one language into another with the translator on this server (works with no internet; 160 languages). Use it whenever the user asks for a translation, how to say something in another language, or what something in another language means. Give the text exactly as it is — do not translate it yourself first.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "The text to translate, exactly as written"},
                    "to": {"type": "string", "description": "The language to translate into: a name or a code, e.g. 'Spanish', 'fr', 'Chinese (Traditional)', 'pt-BR'"},
                    "from": {"type": "string", "description": "The language it is written in, when known (default: detected)"}
                },
                "required": ["text", "to"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "convert_units",
            "description": "Convert a measurement into other units, exactly (arithmetic on the units' definitions, not memory): length, area, volume, weight, temperature, speed, pressure, energy, torque, power, flow, density, fuel economy, data, angles, radiation. Handles compound amounts (5 ft 10 in), compound units (lb/acre, gal/min, kWh/day) and answers in two units (ft in, lb oz). Use it for ANY unit conversion instead of working it out yourself. US gallons/pints/cups unless 'imp'; 'ton' is the US short ton.",
            "parameters": {
                "type": "object",
                "properties": {
                    "amount": {"type": "string", "description": "The number with its unit, e.g. '5 ft 10 in', '72 F', '30 mpg', '2.5 kg', '40 lb/acre'"},
                    "to": {"type": "string", "description": "The unit wanted, e.g. 'cm', 'C', 'L/100km', 'ft in'. Leave out to get the usual units of the other system."}
                },
                "required": ["amount"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "map_position",
            "description": "Map positions, worked out exactly and offline: (1) one place in every notation — latitude/longitude (decimal and degrees-minutes-seconds), UTM and MGRS grid reference, with its elevation and time zone; (2) straight-line distance and true bearing between two places; (3) where you end up going a distance on a bearing. A place may be a town ('Tulsa, OK'), coordinates in any notation ('40.7484, -73.9857', '18T 585632 4511326', '18TWL8563211326'), or 'home' for the user's saved position. Use it for converting coordinates, MGRS/UTM, 'how far is it from A to B', bearings and azimuths.",
            "parameters": {
                "type": "object",
                "properties": {
                    "place": {"type": "string", "description": "One place to describe in every notation"},
                    "from": {"type": "string", "description": "Starting place, for a distance and bearing"},
                    "to": {"type": "string", "description": "Destination place"},
                    "bearing": {"type": "string", "description": "With 'from' and 'distance': the true bearing to travel, in degrees (or e.g. '800 mils')"},
                    "distance": {"type": "string", "description": "With 'from' and 'bearing': how far, with its unit, e.g. '5 km', '2 mi'"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "sun_moon",
            "description": "Sunrise, sunset, first and last light (civil, nautical and astronomical twilight), length of daylight, solar noon, moonrise, moonset and the moon's phase for any place and date — computed offline, good to a minute or two. Also where the sun and moon are in the sky at a given time, and the dates of the next new, quarter and full moons. Use it for 'when does the sun set', 'when does it get dark', 'how much daylight', 'is there a moon tonight', 'when is the next full moon'. Leave 'place' out for the user's saved home position.",
            "parameters": {
                "type": "object",
                "properties": {
                    "place": {"type": "string", "description": "A town ('Denver', 'Paris, TX') or coordinates. Omit for the user's home position."},
                    "date": {"type": "string", "description": "The day: '2026-12-21', 'tomorrow', 'next Friday', 'June 21 2027'. Default: today."},
                    "time": {"type": "string", "description": "Optional time of day ('14:30', '9 pm') to also say where the sun and moon are then"},
                    "days": {"type": "integer", "description": "Optional: a table for this many days (2-14) starting at 'date'"},
                    "zone": {"type": "string", "description": "Optional time zone for the answer (e.g. 'America/Denver', 'UTC', '-07:00'). Default: the place's own."}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "date_math",
            "description": "Calendar arithmetic done by the calendar, not from memory: the number of days (and weeks, months, weekdays) between two dates; the date so many days, weeks, months, years or business days before or after a date; and the day of the week, day of the year and week number of a date. Use it for 'how many days until…', 'what day of the week was…', '90 days from today', 'how old is someone born on…'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "A date: '2027-03-12', 'March 12 2027', 'today', 'next Friday'. Alone: describes that date."},
                    "to": {"type": "string", "description": "A second date: gives the time between 'date' and this one"},
                    "add": {"type": "string", "description": "How far to move 'date': '90 days', '6 weeks', '-3 months', '10 business days', '1 year 2 months'"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "identify_photo",
            "description": "Identify what a photo the user sent shows, properly: a plant, mushroom, insect, spider, snake, bird, animal, track or other sign is put through the species identifier and checked against the offline library (likely name, how sure, dangerous lookalikes, whether it is edible or hazardous); a label, sign or page is read; anything else is described. Use it whenever the user sends or mentions a photo and asks what it is, whether it is safe, edible, poisonous or venomous, or what it says — never name a living thing from a photo yourself. Leave 'photo' out for the picture they sent most recently.",
            "parameters": {
                "type": "object",
                "properties": {
                    "photo": {"type": "string", "description": "The id of the image from the uploaded-files list. Omit for the photo sent most recently."},
                    "note": {"type": "string", "description": "What the user said about it: where and when it was found, what they are asking"},
                    "as": {"type": "string", "description": "Only to correct the sorting: 'text' (it is a label or page — read it), 'describe' (just describe it), 'living' (identify it as a living thing)"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "weather",
            "description": "The weather forecast for the user's home or a named place, from a live weather service: conditions right now, the day's high and low, chance of rain or snow, wind, morning / afternoon / evening, the next 7 days, weather alerts in effect (United States) and air quality. Use it for ANY question about the weather, rain, snow, wind, the temperature outside, a storm, or what to plan or wear for outdoors — instead of web_search. Leave 'place' out for the user's home. If it reports that there is no forecast (the internet is down, or it is switched off), say so plainly; never guess the weather.",
            "parameters": {
                "type": "object",
                "properties": {
                    "place": {"type": "string", "description": "A town ('Denver', 'Paris, TX') or coordinates. Omit for the user's home."},
                    "when": {"type": "string", "description": "'now', 'today', 'tonight', 'tomorrow', a weekday ('Saturday'), a date ('2026-11-28'), 'weekend' or 'week'. Default: today."}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "library_browse",
            "description": "See what the OFFLINE library holds, without searching its text: with no arguments, every category and shelf with how much is on it; with shelf, the collections, books and documents filed there; with find, books and documents by title or author. Use for 'what do you have on …', 'do we have <a book>', 'anything by <an author>', 'show me the cookbooks / the history shelf', and to pick the shelf for library_search. Needs no internet.",
            "parameters": {
                "type": "object",
                "properties": {
                    "shelf": {"type": "string", "description": "A category or shelf to look at, e.g. 'History', 'Cooking/Baking & desserts'"},
                    "find": {"type": "string", "description": "Words from a title or an author's name, e.g. 'moby dick', 'dickens', 'canning'"},
                    "page": {"type": "integer", "description": "Further pages of a long shelf (40 titles a page)"},
                    "all": {"type": "boolean", "description": "true also lists the less-read books the owner's rating cut leaves off the shelves"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "Fetch and read the text content of a specific URL the user names (e.g. 'check example.com', 'what's on this page <url>'). Use when you already have a concrete URL/domain. NOT for open-ended searches (use web_search) or 'research X' jobs (use trigger_research). Downloads are size-budgeted; a '[partial content: ...]' notice in the result means the body was cut short and you can re-call with full=true for the rest.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The URL or domain to fetch (http/https; a bare domain like example.com is fine)"},
                    "full": {"type": "boolean", "description": "Raise the download budget to the hard cap for large pages/files. Use only after a result reported partial content."}
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file from disk. Optionally read a line range with offset/limit for large files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to read"},
                    "offset": {"type": "integer", "description": "1-based line to start reading from (optional)"},
                    "limit": {"type": "integer", "description": "Max number of lines to read from offset (optional)"}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Search file contents for a regular expression across a directory tree (uses ripgrep when available, respecting .gitignore). Returns file:line:match. PREFER this over `bash grep/rg` for code search — confined to the allowed roots, structured output.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regular expression to search for"},
                    "path": {"type": "string", "description": "Directory or file to search (optional; defaults to the project root)"},
                    "glob": {"type": "string", "description": "Only search files matching this glob, e.g. '*.py' (optional)"},
                    "ignore_case": {"type": "boolean", "description": "Case-insensitive match (optional)"},
                    "max_results": {"type": "integer", "description": "Max matches to return (optional)"}
                },
                "required": ["pattern"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "glob",
            "description": "Find files by glob pattern (recursive), newest first. e.g. '**/*.py'. PREFER this over `bash find/ls` for locating files — confined to the allowed roots.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern, e.g. '**/*.ts' or 'src/**/test_*.py'"},
                    "path": {"type": "string", "description": "Base directory (optional; defaults to the project root)"}
                },
                "required": ["pattern"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "ls",
            "description": "List the entries of a directory (folders first, then files with sizes). PREFER this over `bash ls` — confined to the allowed roots.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory to list (optional; defaults to the project root)"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_workspace",
            "description": "Return the absolute path of the active workspace folder the user is working in. File tools are confined to it; the shell starts there but is not sandboxed. Call this first when the user refers to 'the project'/'the code'/'this folder' without a path, instead of asking them. Takes no arguments.",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write/save a file to disk",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to write to"},
                    "content": {"type": "string", "description": "File content to write"}
                },
                "required": ["path", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Edit a file ON DISK by exact string replacement (home folder, project files, any real path like ~/sweden.txt or /path/to/file). This is the right tool for files on disk — NOT edit_document (that's for editor-panel documents). PREFER this over bash (sed/echo) — it shows a diff. old_string must match the file exactly and be unique (or set replace_all). Use write_file to create a new file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to edit"},
                    "old_string": {"type": "string", "description": "Exact text to replace (must match the file, including indentation)"},
                    "new_string": {"type": "string", "description": "Replacement text"},
                    "replace_all": {"type": "boolean", "description": "Replace all occurrences instead of requiring a unique match"}
                },
                "required": ["path", "old_string", "new_string"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "create_document",
            "description": "Create a new document in the editor panel. Use this when the user asks to write, create, build, make, or generate code, scripts, programs, games, apps, or any long-form or structured content that is more than a short paragraph, AND there is no already-open document/email draft that the request refers to. If an email compose draft is open, edit that draft instead of creating another document. NEVER put large generated content directly in chat — use this tool instead.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Document title"},
                    "language": {"type": "string", "description": "Programming language or format (e.g. python, javascript, markdown, text)"},
                    "content": {"type": "string", "description": "The document content"}
                },
                "required": ["title", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "edit_document",
            "description": "Edit a document OPEN IN THE EDITOR PANEL (created via create_document) — NOT a file on disk. For files on disk (home folder, project files, anything with a path like ~/x.txt or /path/to/file) use edit_file instead. Targeted find-and-replace with multiple FIND/REPLACE pairs per call; use for any edit smaller than a full rewrite. Do NOT send the whole file back via update_document for small edits.",
            "parameters": {
                "type": "object",
                "properties": {
                    "edits": {
                        "type": "array",
                        "description": "List of find/replace edits (first match only per edit)",
                        "items": {
                            "type": "object",
                            "properties": {
                                "find": {"type": "string", "description": "Exact text to find in the document"},
                                "replace": {"type": "string", "description": "Text to replace it with"}
                            },
                            "required": ["find", "replace"]
                        }
                    }
                },
                "required": ["edits"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "suggest_document",
            "description": "Suggest improvements to the active document WITHOUT editing it. Creates inline comment bubbles the user can accept or reject. Use when the user asks for suggestions, review, improvements, or feedback.",
            "parameters": {
                "type": "object",
                "properties": {
                    "suggestions": {
                        "type": "array",
                        "description": "List of suggested changes with reasons",
                        "items": {
                            "type": "object",
                            "properties": {
                                "find": {"type": "string", "description": "Exact text in the document to suggest changing"},
                                "replace": {"type": "string", "description": "Suggested replacement text"},
                                "reason": {"type": "string", "description": "Brief explanation of why this change helps"}
                            },
                            "required": ["find", "replace", "reason"]
                        }
                    }
                },
                "required": ["suggestions"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "update_document",
            "description": "Replace the ENTIRE active document. ONLY use for genuine full rewrites (>50% of lines changed). For any smaller change, use edit_document — echoing back the whole file for small edits is wasteful.",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "Complete new document content"}
                },
                "required": ["content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_chats",
            "description": "Search the user's past session transcripts by keyword. Use when the user asks about previous chats, past conversations, or when direct transcript evidence is better than persistent memory. Returns matching sessions with clickable links and nearby context.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search keyword(s) to find in past conversations"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "chat_with_model",
            "description": "Send a message to another AI model and get its response. Use for getting a second opinion, delegating subtasks, or AI-to-AI communication.",
            "parameters": {
                "type": "object",
                "properties": {
                    "model": {"type": "string", "description": "Model name (e.g. 'qwen3-32b') or model@endpoint_name"},
                    "message": {"type": "string", "description": "The message to send to the model"}
                },
                "required": ["model", "message"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "create_session",
            "description": "Create a new chat for ongoing conversations with a specific model. (The UI calls these 'chats'; 'session' is the internal term.)",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Name for the new chat"},
                    "model": {"type": "string", "description": "Model name or model@endpoint_name"}
                },
                "required": ["name", "model"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_sessions",
            "description": "List the user's chats (the UI calls them 'chats') as clickable markdown links. Use this to enumerate chats before opening, renaming, archiving, or deleting them. When replying to the user, preserve the returned [title](#session-id) links; do not strip them into plain text. Optionally filter by keyword.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filter": {"type": "string", "description": "Optional keyword to filter chats by name"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "send_to_session",
            "description": "Send a new message to an existing live chat and get that chat model's response. Do not use this to retrieve, read, summarize, or inspect old chats; use search_chats or list_sessions for past chat evidence.",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "The id of the chat to send the message to"},
                    "message": {"type": "string", "description": "The message to send"}
                },
                "required": ["session_id", "message"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "pipeline",
            "description": "Run a multi-step AI pipeline where each model's output feeds the next. Example: Draft -> Critique -> Revise.",
            "parameters": {
                "type": "object",
                "properties": {
                    "steps": {
                        "type": "array",
                        "description": "Pipeline steps in order",
                        "items": {
                            "type": "object",
                            "properties": {
                                "model": {"type": "string", "description": "Model name for this step"},
                                "instruction": {"type": "string", "description": "What this step should do"}
                            },
                            "required": ["model", "instruction"]
                        }
                    }
                },
                "required": ["steps"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_session",
            "description": "Manage a chat: rename, archive, unarchive, delete, mark important, truncate history, or fork it. (The UI calls these 'chats'; 'session' is the internal term.) For destructive actions like delete, call list_sessions first and pass the exact id returned there; never invent ids.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["rename", "archive", "unarchive", "delete", "important", "unimportant", "truncate", "fork"],
                               "description": "The action to perform"},
                    "session_id": {"type": "string", "description": "Exact target chat id from list_sessions, or 'current' for the active chat where supported"},
                    "value": {"type": "string", "description": "Action parameter: new name (rename), keep_count (truncate/fork)"}
                },
                "required": ["action", "session_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_memory",
            "description": "Manage the user's memory system: list, add, edit, delete, or search memories. Memories persist across sessions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "add", "edit", "delete", "search"],
                               "description": "The action to perform"},
                    "text": {"type": "string", "description": "Memory text (for add/edit) or search query (for search)"},
                    "memory_id": {"type": "string", "description": "Memory ID (for edit/delete)"},
                    "category": {"type": "string",
                                 "enum": ["fact", "event", "contact", "preference", "identity", "interest",
                                          "project", "goal"],
                                 "description": "Memory category (for add/list filter). 'interest' is "
                                                "something the user is into; 'identity' who they are."}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_models",
            "description": "List all available AI models across configured endpoints. Optionally filter by keyword.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filter": {"type": "string", "description": "Optional keyword to filter models"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "ui_control",
            "description": "Control the user interface. Actions: toggle (turn tools on/off), open_panel (open a modal: documents/library, gallery, email, sessions, notes, memories/brain, skills, settings, model_shopper, automations, kitchen), open_email_reply (open an email reply draft document; DOES NOT send. For 'write/draft a reply saying X', include body with the drafted reply), set_mode, switch_model, set_theme (built-in presets: dark, light, midnight, paper, cyberpunk, retrowave, forest, ocean, ume, copper, terminal, organs, lavender, gpt, claude, cute), create_theme (CREATE any custom theme with a name + colors object — pick distinctive, evocative hex colors that match the requested aesthetic, NOT generic defaults. The theme auto-applies after creation). When a user asks for ANY theme not in the built-in preset list, ALWAYS use create_theme.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["toggle", "open_panel", "open_email_reply", "set_mode", "switch_model", "set_theme", "create_theme", "get_toggles"],
                               "description": "The UI action. Use set_theme for presets, create_theme to build a custom theme with any hex colors"},
                    "name": {"type": "string", "description": "For toggle: web, bash, research, incognito, document_editor (aliases: shell, search, deepresearch, documents). For open_panel: documents, gallery, email, sessions, notes, brain/memories, skills, settings, model_shopper, automations, kitchen. For open_email_reply: email UID. For set_theme: a preset theme name. For create_theme: the custom theme name."},
                    "value": {"type": "string", "description": "Value: on/off for toggle, agent/chat for set_mode, model name for switch_model, theme name for set_theme, or folder for open_email_reply"},
                    "uid": {"type": "string", "description": "Email UID for open_email_reply"},
                    "folder": {"type": "string", "description": "Email folder for open_email_reply (default INBOX)"},
                    "mode": {"type": "string", "description": "Reply draft mode for open_email_reply: reply, reply-all, or ai-reply"},
                    "body": {"type": "string", "description": "For open_email_reply: reply body to pre-fill. Required whenever the user told you what the reply should say. Opens a draft, does not send."},
                    "colors": {"type": "object", "description": "For create_theme: the theme colors",
                               "properties": {
                                   "bg": {"type": "string", "description": "Background color (hex, e.g. #1a1a2e)"},
                                   "fg": {"type": "string", "description": "Foreground/text color (hex)"},
                                   "panel": {"type": "string", "description": "Panel/sidebar background color (hex)"},
                                   "border": {"type": "string", "description": "Border/divider color (hex)"},
                                   "accent": {"type": "string", "description": "Accent color for buttons, brand, highlights (hex)"},
                                   "userBubbleBg": {"type": "string", "description": "User chat bubble background (hex, optional)"},
                                   "aiBubbleBg": {"type": "string", "description": "AI chat bubble background (hex, optional)"},
                                   "bubbleBorder": {"type": "string", "description": "Chat bubble border color (hex, optional)"},
                                   "sidebarBg": {"type": "string", "description": "Sidebar background override (hex, optional)"},
                                   "sectionAccent": {"type": "string", "description": "Section header accent color (hex, optional)"},
                                   "brandColor": {"type": "string", "description": "Brand/logo color (hex, optional)"},
                                   "inputBg": {"type": "string", "description": "Chat input background (hex, optional)"},
                                   "inputBorder": {"type": "string", "description": "Chat input border (hex, optional)"},
                                   "sendBtnBg": {"type": "string", "description": "Send button background (hex, optional)"},
                                   "sendBtnHover": {"type": "string", "description": "Send button hover color (hex, optional)"},
                                   "codeBg": {"type": "string", "description": "Code block background (hex, optional)"},
                                   "codeFg": {"type": "string", "description": "Code block text color (hex, optional)"},
                                   "toggleBg": {"type": "string", "description": "Toggle switch off background (hex, optional)"},
                                   "toggleActive": {"type": "string", "description": "Toggle switch on color (hex, optional)"},
                                   "accentPrimary": {"type": "string", "description": "Primary accent override (hex, optional)"},
                                   "accentError": {"type": "string", "description": "Error/danger color (hex, optional)"}
                               },
                               "required": ["bg", "fg", "panel", "border", "accent"]}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "ask_user",
            "description": "Ask the user a multiple-choice question to get a decision or clarification when the task is genuinely ambiguous and the answer changes what you do next (e.g. pick between approaches, confirm an assumption, choose a target). The user sees clickable option buttons; calling this ENDS your turn and their selection arrives as your next message. Prefer sensible defaults over asking — only ask when you truly cannot proceed well without the user's input. Do NOT use it to confirm irreversible/destructive actions that have a dedicated confirmation flow.",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "The question to ask. Be specific and self-contained."},
                    "options": {
                        "type": "array",
                        "description": "2-6 choices. Each is an object with a short `label` and an optional `description` explaining the trade-off.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string", "description": "Concise choice text the user clicks (1-5 words)."},
                                "description": {"type": "string", "description": "Optional one-line explanation of this choice."}
                            },
                            "required": ["label"]
                        }
                    },
                    "multi": {"type": "boolean", "description": "Set true ONLY when the question explicitly allows choosing more than one option. Otherwise omit it or set false. Default false."}
                },
                "required": ["question", "options"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "update_plan",
            "description": "Write back to the ACTIVE PLAN: mark steps done or revise them. Use this while executing an approved plan — after you finish a step, call update_plan with the full checklist and that step marked `- [x]`; when the user asks to change the plan, call it with the revised checklist. The user's docked plan window updates live. Pass the COMPLETE checklist every time (not a diff). No effect if there is no active plan.",
            "parameters": {
                "type": "object",
                "properties": {
                    "plan": {"type": "string", "description": "The full updated plan as a GitHub-style markdown checklist — one step per line, `- [ ]` for pending and `- [x]` for done. Always send the whole list."}
                },
                "required": ["plan"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_tasks",
            "description": "Manage scheduled/automated tasks: list, create, edit, delete, pause, resume, or run tasks. Use this for ANY recurring/scheduled request ('every morning…', 'each day at 7:30', 'daily summarize…') — create a task rather than doing it once. Task types: llm (AI runs a prompt), research (runs the deep-research pipeline on a question), or action (built-in automation). Triggers can be time-based or event-based.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "create", "edit", "delete", "pause", "resume", "run"],
                               "description": "The action to perform"},
                    "task_id": {"type": "string", "description": "Task ID (for edit/delete/pause/resume/run)"},
                    "name": {"type": "string", "description": "Task name"},
                    "prompt": {"type": "string", "description": "The instruction (for task_type=llm) or the research question (for task_type=research). Required for both."},
                    "task_type": {"type": "string", "enum": ["llm", "research", "action"],
                                  "description": "llm = AI runs your prompt; research = runs the deep-research pipeline on the prompt as a question; action = direct built-in function"},
                    "action_name": {"type": "string", "enum": [
                        "tidy_sessions", "tidy_documents", "consolidate_memory", "tidy_research",
                        "summarize_emails", "draft_email_replies", "extract_email_events",
                        "classify_events", "learn_sender_signatures",
                        "test_skills", "audit_skills", "check_email_urgency"
                    ],
                                    "description": "Built-in action (for task_type=action)"},
                    "trigger_type": {"type": "string", "enum": ["schedule", "event"],
                                     "description": "schedule = time-based, event = count-based"},
                    "schedule": {"type": "string", "enum": ["once", "daily", "weekly", "monthly"],
                                 "description": "Schedule frequency (for trigger_type=schedule)"},
                    "scheduled_time": {"type": "string", "description": "HH:MM in UTC (for schedule triggers). Convert the user's stated local time using the UTC offset given in the 'Current date and time' context."},
                    "scheduled_day": {"type": "integer", "description": "Day of week 0=Mon (weekly) or day of month (monthly)"},
                    "trigger_event": {"type": "string", "enum": ["session_created", "message_sent", "document_created", "memory_added", "research_completed", "email_received", "skill_added"],
                                      "description": "Event name (for trigger_type=event)"},
                    "trigger_count": {"type": "integer", "description": "Fire every N events (for trigger_type=event)"},
                    "output_target": {"type": "string", "description": "Where results go. Defaults to 'session' (results land in a dedicated chat session the user reads) — this is the right choice for 'summarize for me' / 'send to me'. Do NOT go hunting for the user's email address; only use an email MCP tool name here if the user explicitly asked to be emailed AND an address is already known."}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_calendar",
            "description": "Manage calendar events: list events in a date range, create, update, delete. Each event can carry a tag/category (event_type) and importance level. Resolve relative dates like today/tomorrow against the 'Current date and time' system context, then pass ISO 8601 datetimes in the user's local wall time; for all-day events set all_day=true and pass YYYY-MM-DD. For event reminders/alarms, pass reminder_minutes; the tool creates the Aegis note reminder, so do not also call manage_notes for the same reminder. Do not set rrule for single-occurrence requests such as 'next Wednesday only'; use rrule only when the user explicitly wants recurrence.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string",
                               "enum": ["list_events", "create_event", "update_event", "delete_event", "list_calendars"],
                               "description": "Action to perform"},
                    "summary": {"type": "string", "description": "Event title (for create/update)"},
                    "dtstart": {"type": "string", "description": "Start ISO datetime, or YYYY-MM-DD if all_day"},
                    "dtend": {"type": "string", "description": "End ISO datetime; defaults to +1h (or +1 day for all_day)"},
                    "all_day": {"type": "boolean", "description": "Whether this is an all-day event"},
                    "description": {"type": "string", "description": "Event description / notes"},
                    "location": {"type": "string", "description": "Event location"},
                    "uid": {"type": "string", "description": "Event UID (for update/delete)"},
                    "calendar_href": {"type": "string", "description": "Specific calendar URL (optional; defaults to first calendar)"},
                    "calendar": {"type": "string", "description": "Filter list_events by calendar name or href"},
                    "start": {"type": "string", "description": "list_events range start (ISO datetime). Use this for month/week requests after resolving the date range; do not pass a loose query string. Prefer start; backend also accepts start_time, start_date, range_start, from, dtstart, since."},
                    "end": {"type": "string", "description": "list_events range end (ISO datetime). Use this for month/week requests after resolving the date range; defaults to +14 days only when no range is requested. Prefer end; backend also accepts end_time, end_date, range_end, to, dtend, until."},
                    "event_type": {"type": "string", "description": "Tag / category for the event. Common values: work, personal, health, travel, meal, social, admin, other. Aliases accepted: tag, category, type."},
                    "importance": {"type": "string", "enum": ["low", "normal", "high", "critical"], "description": "Priority level (defaults to 'normal')"},
                    "reminder_minutes": {"type": "integer", "description": "For create_event: create an Aegis reminder this many minutes before the event, e.g. 5 for 'reminder 5 min before'."},
                    "rrule": {"type": "string", "description": "Recurrence rule in iCalendar RRULE format, e.g. 'FREQ=WEEKLY;BYDAY=MO' for weekly on Monday. Use with create_event or update_event. For update_event, pass an explicit empty string to remove recurrence and make the event single-occurrence."}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_notes",
            "description": "Manage notes and checklists (Google Keep-style): list, view, add, update, delete, toggle_item. Use list/search to find candidate notes, then view with the note id when you need the full body. IMPORTANT: For to-do lists / checklists, set note_type='checklist' and pass the items as the `checklist_items` array — do NOT serialize them into `content` as plain text. For freeform notes, use note_type='note' and put the body in `content`. `due_date` accepts natural language like 'tomorrow at 9am' (parsed in the user's timezone) and fires a notification — do not also create a calendar event for the same reminder.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string",
                               "enum": ["list", "search", "view", "add", "update", "delete", "toggle_item"],
                               "description": "The action to perform"},
                    "id": {"type": "string", "description": "Note id (for update/delete/toggle_item); 8-char prefix is fine"},
                    "query": {"type": "string", "description": "Search text for action='search'"},
                    "title": {"type": "string", "description": "Note title (for add/update)"},
                    "content": {"type": "string", "description": "Freeform body text. Use this for note_type='note'. Do NOT use this for checklists — pass `checklist_items` instead."},
                    "note_type": {"type": "string", "enum": ["note", "checklist"],
                                  "description": "'note' = freeform text in `content`. 'checklist' = structured to-do items in `checklist_items`. Defaults to 'checklist' if checklist_items is supplied, else 'note'."},
                    "checklist_items": {"type": "array",
                                        "items": {"type": "object",
                                                  "properties": {
                                                      "text": {"type": "string", "description": "The to-do item text"},
                                                      "done": {"type": "boolean", "description": "Whether the item is checked off"}
                                                  },
                                                  "required": ["text"]},
                                        "description": "Checklist items for note_type='checklist'. Each item is {text, done}. REQUIRED for checklists — leaving this empty produces a blank note."},
                    "color": {"type": "string", "description": "Optional color label (e.g. 'yellow', 'blue', 'green')"},
                    "label": {"type": "string", "description": "Optional category label (also used as a list filter)"},
                    "pinned": {"type": "boolean", "description": "Pin the note to the top"},
                    "archived": {"type": "boolean", "description": "For update: archive/unarchive. For list: show archived notes when true."},
                    "due_date": {"type": "string", "description": "Reminder time. Accepts natural language ('tomorrow at 9am', '11pm today') or ISO 8601. Fires a notification at that time."},
                    "index": {"type": "integer", "description": "Checklist item index (for toggle_item, 0-based)"}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "api_call",
            "description": "Call a registered API integration (RSS reader, git forge, bookmark manager, smart home, etc.). Check the system context for available integrations and their endpoints.",
            "parameters": {
                "type": "object",
                "properties": {
                    "integration": {"type": "string", "description": "Integration name or ID (e.g. 'Miniflux', 'Gitea')"},
                    "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"], "description": "HTTP method"},
                    "path": {"type": "string", "description": "API endpoint path (e.g. '/v1/entries?status=unread&limit=20')"},
                    "body": {"type": "object", "description": "JSON request body (for POST/PUT/PATCH)"}
                },
                "required": ["integration", "method", "path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "ask_teacher",
            "description": "Ask a more capable AI model for help when stuck on a difficult problem. The teacher provides guidance that can be saved as a learned skill.",
            "parameters": {
                "type": "object",
                "properties": {
                    "model": {"type": "string", "description": "Teacher model name (e.g. 'claude-sonnet-4') or 'auto' for configured default"},
                    "problem": {"type": "string", "description": "Describe the problem or question you need help with"}
                },
                "required": ["problem"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_skills",
            "description": (
                "Read or modify the user's skill library. Skills are SKILL.md files "
                "(YAML frontmatter + structured body: When to Use / Procedure / "
                "Pitfalls / Verification) and follow a draft → published lifecycle. "
                "Use progressive disclosure: 'list' to see what exists, 'view' to "
                "load full content for a single skill, 'view_ref' for sub-files. "
                "Use 'patch' for surgical text edits and 'edit' for full rewrites. "
                "'publish' once you've verified the procedure works. For add, "
                "always provide an explicit name slug and only tell the user the "
                "exact name returned by the tool."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "view", "view_ref", "add", "edit", "patch", "publish", "delete", "search"], "description": "list = name+description summary; view = full SKILL.md; view_ref = sub-file under the skill dir; add = create; edit = full rewrite (content); patch = old_string→new_string; publish = flip status; delete; search = relevance match on published skills."},
                    "name": {"type": "string", "description": "Slug/name of the skill. Required for add/view/view_ref/edit/patch/publish/delete. For add, choose the exact kebab-case name the user should see and report only the returned name."},
                    "path": {"type": "string", "description": "Sub-path under the skill directory for view_ref (e.g. 'references/example.md')."},
                    "description": {"type": "string", "description": "One-line summary surfaced in the skills index (for add)."},
                    "category": {"type": "string", "description": "Organizational grouping like 'dev', 'email', 'system' (for add)."},
                    "when_to_use": {"type": "string", "description": "Trigger conditions in plain English (for add)."},
                    "procedure": {"type": "array", "items": {"type": "string"}, "description": "Numbered steps (for add)."},
                    "pitfalls": {"type": "array", "items": {"type": "string"}, "description": "Known failure modes + recovery (for add)."},
                    "verification": {"type": "array", "items": {"type": "string"}, "description": "How to confirm the procedure succeeded (for add)."},
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "Keyword tags (for add)."},
                    "platforms": {"type": "array", "items": {"type": "string"}, "description": "Restrict to OSes (for add)."},
                    "requires_toolsets": {"type": "array", "items": {"type": "string"}, "description": "Hide unless these toolsets are active (for add)."},
                    "fallback_for_toolsets": {"type": "array", "items": {"type": "string"}, "description": "Hide when these toolsets are active (for add)."},
                    "status": {"type": "string", "enum": ["draft", "published"], "description": "Defaults to 'draft' on add."},
                    "version": {"type": "string", "description": "Semver-ish, e.g. '1.0.0' (for add)."},
                    "confidence": {"type": "number", "description": "0-1 (for add/publish)."},
                    "content": {"type": "string", "description": "Full SKILL.md text (for edit)."},
                    "old_string": {"type": "string", "description": "Exact substring to replace (for patch). Must appear exactly once."},
                    "new_string": {"type": "string", "description": "Replacement text (for patch)."},
                    "query": {"type": "string", "description": "Search query (for search)."}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_endpoints",
            "description": "Manage model API endpoints: list configured endpoints, add new ones, delete, enable or disable them.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "add", "delete", "enable", "disable"]},
                    "endpoint_id": {"type": "string", "description": "Endpoint ID (for delete/enable/disable)"},
                    "name": {"type": "string", "description": "Display name (for add)"},
                    "base_url": {"type": "string", "description": "API base URL e.g. https://api.openai.com/v1 (for add)"},
                    "api_key": {"type": "string", "description": "API key (for add)"}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_mcp",
            "description": "Manage MCP (Model Context Protocol) tool servers: list servers and their tools, add new servers, delete, enable/disable, reconnect, or list all available tools.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "add", "delete", "enable", "disable", "reconnect", "list_tools"]},
                    "server_id": {"type": "string", "description": "Server ID (for delete/enable/disable/reconnect)"},
                    "name": {"type": "string", "description": "Server name (for add)"},
                    "command": {"type": "string", "description": "Command to run e.g. npx (for add)"},
                    "args": {"type": "array", "items": {"type": "string"}, "description": "Command arguments (for add)"},
                    "env": {"type": "object", "description": "Environment variables (for add)"}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_webhooks",
            "description": "Manage webhooks: list, add, delete, enable or disable webhook endpoints.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "add", "delete", "enable", "disable"]},
                    "webhook_id": {"type": "string", "description": "Webhook ID (for delete/enable/disable)"},
                    "name": {"type": "string", "description": "Webhook name (for add)"},
                    "url": {"type": "string", "description": "Webhook URL (for add)"},
                    "events": {"type": "string", "description": "Comma-separated event names (for add)"}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_tokens",
            "description": "Manage API access tokens: list existing tokens, create new ones, or delete them.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "create", "delete"]},
                    "token_id": {"type": "string", "description": "Token ID (for delete)"},
                    "name": {"type": "string", "description": "Token name (for create)"}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_documents",
            "description": "Manage documents: list all documents (with optional search/language filter), delete documents, or run tidy cleanup.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "delete", "tidy"]},
                    "document_id": {"type": "string", "description": "Document ID (for delete)"},
                    "search": {"type": "string", "description": "Search query (for list)"},
                    "language": {"type": "string", "description": "Filter by language (for list)"},
                    "limit": {"type": "integer", "description": "Max results (for list, default 50)"}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_settings",
            "description": "Manage user preferences and settings. Use `disable_tool`/`enable_tool`/`list_tools` to turn individual tools on or off globally (e.g. shell, search, browser, documents, memory, skills, images, tasks, notes, calendar, email). Use list/get/set/delete for free-form preferences.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "get", "set", "delete", "disable_tool", "enable_tool", "list_tools"]},
                    "key": {"type": "string", "description": "Setting key (for get/set/delete)"},
                    "value": {"description": "Setting value (for set) — can be string, number, boolean, or object"},
                    "tool": {"type": "string", "description": "Tool name to disable/enable (for disable_tool/enable_tool). Accepts aliases: shell, search, browser, documents, memory, skills, images, tasks, notes, calendar, email — or a raw tool name like 'bash' or 'web_search'."}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "download_model",
            "description": "Download a HuggingFace model to a server. If `host` is omitted, defaults to the Model Shopper's currently-selected server (NOT localhost) — call list_cookbook_servers first if you're unsure where it should go.",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo_id": {"type": "string", "description": "HuggingFace repo (e.g. 'Qwen/Qwen3-8B')"},
                    "host": {"type": "string", "description": "Target server — use the friendly NAME from list_cookbook_servers (e.g. 'gpu-box', 'workstation') or a raw user@host. Omit to use the Model Shopper's selected default server."},
                    "local": {"type": "boolean", "description": "Force download to THIS machine (localhost) instead of the default remote server."},
                    "include": {"type": "string", "description": "Glob filter for specific files (e.g. '*Q4_K_M*')"},
                },
                "required": ["repo_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "serve_model",
            "description": "Start serving a model with vLLM, SGLang, llama.cpp, Ollama, or Diffusers. If `host` is omitted, defaults to the Model Shopper's selected server (not localhost). For image/inpainting/diffusion models use the built-in command `python3 scripts/diffusion_server.py --model <repo> --port 8100` rather than inventing a custom diffusers API server. After launching, call list_served_models to check readiness/errors; if it reports a diagnosis with retry suggestions, retry via serve_model using the suggested adjusted cmd.",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo_id": {"type": "string", "description": "Model repo (e.g. 'Qwen/Qwen3-8B')"},
                    "cmd": {"type": "string", "description": "Full serve command (e.g. 'vllm serve Qwen/Qwen3-8B --port 8000 --tp 2', 'python3 -m sglang.launch_server --model-path Qwen/Qwen3-8B --port 30000', or for inpainting/image models: 'python3 scripts/diffusion_server.py --model diffusers/stable-diffusion-xl-1.0-inpainting-0.1 --port 8100')"},
                    "host": {"type": "string", "description": "Target server — friendly NAME from list_cookbook_servers (e.g. 'gpu-box', 'workstation') or raw user@host. Omit to use the Model Shopper's selected default."},
                    "local": {"type": "boolean", "description": "Force serve on THIS machine instead of the default remote server."},
                },
                "required": ["repo_id", "cmd"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_served_models",
            "description": "List currently running model servers with status, model name, port, throughput, and structured Model Shopper diagnoses. If a serve failed, this includes recent logs plus retry suggestions/adjusted commands the agent can use with serve_model.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "stop_served_model",
            "description": "Stop a running model server.",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "Tmux session ID of the server to stop"},
                },
                "required": ["session_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "tail_serve_output",
            "description": "Read the last N lines of a Model Shopper serve/download task's tmux pane. Use ONLY in this exact sequence: (1) the user asked to serve a model, (2) you launched it via serve_model, (3) list_served_models reports the NEW task as crashed/error, (4) call tail_serve_output on the new sessionId to find the root cause, (5) call serve_model again with adjusted flags. DO NOT call this on old stopped/completed download tasks — they are historical and won't tell you anything about the current attempt. DO NOT investigate past failures before launching; the environment may have changed since.",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "Tmux session id from list_served_models (e.g. 'serve-abc12345', 'cookbook-a1b2c3d4')."},
                    "tail": {"type": "integer", "description": "How many lines of pane scrollback to fetch (default 300, max 4000). Bump this if the error in the visible tail references an earlier line ('see root cause above')."},
                },
                "required": ["session_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_downloads",
            "description": "List in-progress model downloads in the Model Shopper. Shows each download's model name, phase, percent (if available), session ID, and remote host.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_download",
            "description": "Cancel an in-progress model download by killing its tmux session. Use list_downloads first to get the session_id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "Tmux session ID from list_downloads (e.g. 'cookbook-a1b2c3d4')"},
                },
                "required": ["session_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_hf_models",
            "description": "Search HuggingFace for models matching a query. Returns a ranked list of repo IDs, sizes (when available), and download counts. Use this when the user wants to find a model to download.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search terms (e.g. 'Qwen 8B', 'flux', 'llama-3 instruct')"},
                    "limit": {"type": "integer", "description": "Max results (default 10)"},
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_cookbook_servers",
            "description": "List the Model Shopper's configured servers (remote GPU boxes + local) and the current default host. Call this before download_model/serve_model when the user didn't specify a host, so models go to the right machine (where the GPUs and model cache are) instead of localhost. If multiple servers and intent is ambiguous, show them and ask the user which.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_serve_presets",
            "description": "List saved Model Shopper serve presets. Each preset is a launch template (name, model, host, port, tmux cmd) the user previously saved from the UI. Call this BEFORE serve_model when the user asks to launch a model by name — there's almost always a working preset for it.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "adopt_served_model",
            "description": "Register an existing tmux model server (started manually or outside the Model Shopper) into Model Shopper tracking, AND add it as a chat endpoint. Use when the user (or you) launched something via ssh+tmux and now want it visible in the UI / stoppable via stop_served_model / usable in the model picker. Verifies the tmux session + port respond before adding.",
            "parameters": {
                "type": "object",
                "properties": {
                    "host": {"type": "string", "description": "Remote host in user@host form (e.g. 'user@192.0.2.10'). Omit for localhost."},
                    "tmux_session": {"type": "string", "description": "Existing tmux session name (e.g. 'minimax-m27')"},
                    "model": {"type": "string", "description": "Model repo_id or display name (e.g. 'cyankiwi/MiniMax-M2.7-AWQ-4bit')"},
                    "port": {"type": "integer", "description": "Port the server is listening on (default 8000)"},
                    "name": {"type": "string", "description": "Optional display name (defaults to model basename)"},
                    "add_endpoint": {"type": "boolean", "description": "Also register as a chat endpoint (default true)"}
                },
                "required": ["tmux_session", "model"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "serve_preset",
            "description": "Launch a saved Model Shopper serve preset by name. Reuses the exact tmux command + host the user saved before. This is the preferred way to start a known model (SD3.5, vLLM presets, etc.) — don't fabricate launch commands when a preset exists.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Preset name (exact or case-insensitive substring of one returned by list_serve_presets)"},
                },
                "required": ["name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_cached_models",
            "description": "List models already cached on disk locally or on a remote server. `host` accepts friendly Model Shopper server names from list_cookbook_servers (for example ajax) or raw user@host. Also reports completed Model Shopper download tasks when the filesystem cache scan cannot locate the HF cache path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "host": {"type": "string", "description": "Friendly Model Shopper server name (e.g. 'ajax', 'gpu-box') or raw remote host (e.g. 'user@gpu-box'). Omit for local."},
                    "model_dir": {"type": "string", "description": "Comma-separated additional model directories to scan beyond ~/.cache/huggingface/hub"},
                    "ssh_port": {"type": "string", "description": "SSH port for remote host (default 22)"},
                    "platform": {"type": "string", "enum": ["linux", "windows"], "description": "Remote platform"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "app_api",
            "description": "Generic loopback to allowed internal Aegis endpoints. Use this when there's no named tool for what the user wants. Hits the same routes the UI buttons hit (the Model Shopper under /api/cookbook, gallery, library/documents, memory, notes, calendar, tasks, settings, themes, research, compare, etc.). action='endpoints' returns the OpenAPI surface (use `filter` to narrow). action='call' (default) takes method+path+body. Sensitive auth/user/admin/shell paths and host-control Model Shopper mutation routes are blocked for safety. Do not use for shell commands; use named command tooling instead. Do not use for package installs, engine rebuilds, PID signalling, or email account discovery; use list_email_accounts for email accounts because /api/email/accounts is owner-filtered in tool context.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["call", "endpoints"], "description": "'call' to hit an endpoint, 'endpoints' to list what's available"},
                    "path": {"type": "string", "description": "Endpoint path starting with /api/ (e.g. '/api/cookbook/gpus', '/api/gallery/list', '/api/calendar/events')"},
                    "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"], "description": "HTTP method (default GET)"},
                    "body": {"type": "object", "description": "JSON request body for POST/PUT/PATCH"},
                    "query": {"type": "object", "description": "Querystring params as a key-value object"},
                    "filter": {"type": "string", "description": "For action=endpoints: substring to filter paths/summaries (e.g. 'cookbook', 'gallery')"}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            # Without this schema, native-tool models get the generate_image
            # PROMPT section (tool-RAG selects it) but no callable schema — so
            # they substitute the nearest schema they do have (manage_memory)
            # and loop until the round cap. Keep params in sync with
            # mcp_servers/image_gen_server.py.
            "name": "generate_image",
            "description": "Generate an AI image from a text prompt using the configured image model (e.g. the local qwen-image diffusion pipeline). Returns a link to the saved image. Use for any 'draw/create/generate a picture/image/logo/photo' request.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "Image description prompt"},
                    "model": {"type": "string", "description": "Image model name (omit to use the configured default)"},
                    "size": {"type": "string", "description": "Image size WxH, e.g. 768x768 (invalid sizes are normalized per model)"},
                    "quality": {"type": "string", "enum": ["low", "medium", "high", "auto"], "description": "Quality (gpt-image models only)"},
                },
                "required": ["prompt"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "edit_image",
            "description": "Edit a gallery image: upscale, remove background, inpaint, or harmonize.",
            "parameters": {
                "type": "object",
                "properties": {
                    "image_id": {"type": "string", "description": "Gallery image ID"},
                    "action": {"type": "string", "enum": ["upscale", "rembg", "inpaint", "harmonize"], "description": "Edit action"},
                    "prompt": {"type": "string", "description": "For inpaint: what to fill the masked area with"},
                    "scale": {"type": "number", "description": "For upscale: scale factor (default 2)"},
                },
                "required": ["image_id", "action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_research",
            "description": "List, read, or delete saved deep-research reports from the Library. Use action=list to find the most recent report id, then action=read with that id to get the report text. This is THE way to read a finished research report.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "read", "delete"], "description": "list = recent reports, read = report text by id, delete = remove by id"},
                    "id": {"type": "string", "description": "Report id (required for read/delete)"},
                    "search": {"type": "string", "description": "For list: filter by query text"},
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "trigger_research",
            "description": "Start a deep research task on a topic. Returns a task ID for tracking.",
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string", "description": "Research question or topic"},
                },
                "required": ["topic"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "resolve_contact",
            "description": "Look up a contact by name. Searches CardDAV address book and sent email history. Returns email addresses (when available) or phone numbers. Use when the user says 'message [name]', 'email [name]', or asks for someone's contact details.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Person's name to search for"},
                },
                "required": ["name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_contact",
            "description": "Create, update, delete, or list the user's CardDAV contacts. Use to save a new contact, update an existing one (email/phone/address), or remove one. Add does not require email: name + phone or name + address is valid. For update/delete you need the contact's uid — call action='list' first to find it. Writes go through the same dedupe + validation as the Contacts UI.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "add", "update", "delete"],
                               "description": "list = show all contacts (with uids); add = create; update = edit by uid; delete = remove by uid."},
                    "uid": {"type": "string", "description": "Contact UID (required for update/delete; get it from action=list)."},
                    "name": {"type": "string", "description": "Contact's display name (for add/update)."},
                    "email": {"type": "string", "description": "Single email address (convenience for add, or the primary email for update). Optional when phone or address is provided."},
                    "emails": {"type": "array", "items": {"type": "string"}, "description": "Full list of email addresses (first is primary)."},
                    "phones": {"type": "array", "items": {"type": "string"}, "description": "Full list of phone numbers. Valid for add/update."},
                    "address": {"type": "string", "description": "Postal/mailing address as a single human-readable string."},
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_email_accounts",
            "description": "List configured email accounts. Use this before checking mail when the user names a mailbox/account such as Gmail, work, or a custom domain, then pass the returned account name/email/id to the other email tools.",
            "parameters": {
                "type": "object",
                "properties": {},
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "send_email",
            "description": "Send a new email. Use resolve_contact first if you only have a name and need to find the email address. If multiple accounts exist, pass account from list_email_accounts.",
            "parameters": {
                "type": "object",
                "properties": {
                    "to": {"type": "string", "description": "Recipient email address"},
                    "subject": {"type": "string", "description": "Email subject line"},
                    "body": {"type": "string", "description": "Email body text"},
                    "account": {"type": "string", "description": "Optional account name/email/id from list_email_accounts, e.g. Gmail or user@example.com"},
                },
                "required": ["to", "subject", "body"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_emails",
            "description": "List emails from an account/folder, newest first. Returns subject, sender, date, UID, and account for each email. Use list_email_accounts first when the user mentions Gmail/work/a custom mailbox. For last/latest/newest email requests, use max_results=1 and unread_only=false.",
            "parameters": {
                "type": "object",
                "properties": {
                    "folder": {"type": "string", "description": "IMAP folder (default: INBOX)"},
                    "max_results": {"type": "integer", "description": "Max emails to return (default: 20)"},
                    "limit": {"type": "integer", "description": "Backward-compatible alias for max_results"},
                    "unread_only": {"type": "boolean", "description": "Only show unread emails. Default false; set true only when the user asks for unread emails."},
                    "unresponded_only": {"type": "boolean", "description": "Only show unanswered emails. Default false."},
                    "account": {"type": "string", "description": "Optional account name/email/id from list_email_accounts, e.g. Gmail or user@example.com"},
                },
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_email",
            "description": "Read the full content of a specific email by UID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "uid": {"type": "string", "description": "Email UID to read"},
                    "folder": {"type": "string", "description": "IMAP folder (default: INBOX)"},
                    "account": {"type": "string", "description": "Optional account name/email/id from list_email_accounts, especially when the UID came from a non-default mailbox"},
                },
                "required": ["uid"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "reply_to_email",
            "description": "SEND a reply email immediately by UID. Do not use this when the user asks to write/draft/open/start a reply; use ui_control action=open_email_reply with body instead so the user can review. Only use when the user explicitly says to send now. Use the exact UID from the latest read_email/list_emails result; never invent UID 1. Automatically threads with In-Reply-To/References headers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "uid": {"type": "string", "description": "Exact UID of the email to reply to from list_emails/read_email; never invent UID 1"},
                    "body": {"type": "string", "description": "Reply body text"},
                    "folder": {"type": "string", "description": "IMAP folder (default: INBOX)"},
                    "account": {"type": "string", "description": "Optional account name/email/id from list_email_accounts, especially when the UID came from a non-default mailbox"},
                },
                "required": ["uid", "body"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "bulk_email",
            "description": "Perform one action on many emails at once. Use this for 'delete all those', 'archive these', 'mark all read', or any bulk operation after list_emails. Always pass account when the listed emails came from a named account such as Gmail. Spam is in the folder 'Spam': list that folder and act on its messages; never treat INBOX messages as spam because the spam folder could not be opened. Deleting more than 20 messages in one call needs the person's say-so: say how many and what they are, ask, and only then pass confirmed_count.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["mark_read", "mark_unread", "archive", "delete", "junk"], "description": "Bulk action to perform"},
                    "uids": {"type": "array", "items": {"type": "string"}, "description": "UIDs from the latest list_emails result"},
                    "all_unread": {"type": "boolean", "description": "Operate on all unread messages in folder instead of explicit UIDs"},
                    "folder": {"type": "string", "description": "IMAP folder (default: INBOX)"},
                    "permanent": {"type": "boolean", "description": "For delete: hard-delete instead of moving to Trash"},
                    "confirmed_count": {"type": "integer", "description": "For delete of more than 20 messages: the number of messages, passed only after the person was told how many and which, and said yes"},
                    "account": {"type": "string", "description": "Account name/email/id from list_email_accounts, e.g. Gmail or user@example.com"},
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delete_email",
            "description": "Delete one email by UID. For multiple messages, use bulk_email instead. Always pass account when the email came from a named account such as Gmail.",
            "parameters": {
                "type": "object",
                "properties": {
                    "uid": {"type": "string", "description": "Email UID from list_emails/read_email"},
                    "folder": {"type": "string", "description": "IMAP folder (default: INBOX)"},
                    "permanent": {"type": "boolean", "description": "Hard-delete instead of moving to Trash"},
                    "account": {"type": "string", "description": "Account name/email/id from list_email_accounts"},
                },
                "required": ["uid"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "archive_email",
            "description": "Archive one email by UID. For multiple messages, use bulk_email instead. Always pass account when the email came from a named account such as Gmail.",
            "parameters": {
                "type": "object",
                "properties": {
                    "uid": {"type": "string", "description": "Email UID from list_emails/read_email"},
                    "folder": {"type": "string", "description": "IMAP folder (default: INBOX)"},
                    "account": {"type": "string", "description": "Account name/email/id from list_email_accounts"},
                },
                "required": ["uid"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "mark_email_read",
            "description": "Mark one email as read or unread by UID. For multiple messages, use bulk_email instead. Always pass account when the email came from a named account such as Gmail.",
            "parameters": {
                "type": "object",
                "properties": {
                    "uid": {"type": "string", "description": "Email UID from list_emails/read_email"},
                    "folder": {"type": "string", "description": "IMAP folder (default: INBOX)"},
                    "read": {"type": "boolean", "description": "True marks read; false marks unread"},
                    "account": {"type": "string", "description": "Account name/email/id from list_email_accounts"},
                },
                "required": ["uid"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_lists",
            "description": "The user's lists and plans (shopping, packing, to-do, a plan for an event). add puts items on a list by name and creates the list if it does not exist; check / uncheck tick items off by what they say; remove takes items off; show reads a list back; list names every list. Use this whenever the user wants something put on, taken off or read from a list.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["add", "show", "check", "uncheck", "remove", "clear_done", "list", "rename", "delete"]},
                    "list": {"type": "string", "description": "The list's name as the user says it, e.g. 'groceries'"},
                    "items": {"type": "array", "items": {"type": "string"}, "description": "One entry per item, e.g. ['milk', 'eggs']"},
                    "kind": {"type": "string", "enum": ["list", "plan"], "description": "add: 'plan' for a step-by-step plan toward something"},
                    "new_name": {"type": "string", "description": "rename: the new name"}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_kitchen",
            "description": "The user's kitchen cookbook: food recipes to cook from. save sets a recipe out for cooking: kept as written, measures worked out for today (old measures and oven words translated and marked as estimates; cups also in grams, Fahrenheit also in Celsius), the method broken into steps to follow one at a time; returns the ingredients, the steps and a link to the follow-along view. show reads a saved recipe back, list names them, delete removes one.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["save", "show", "list", "delete"]},
                    "text": {"type": "string", "description": "save: the recipe exactly as written, unchanged"},
                    "url": {"type": "string", "description": "save: a link to the recipe instead of its text (a web page, or a library link)"},
                    "title": {"type": "string", "description": "save: what to call it, if the recipe has no name of its own"},
                    "recipe": {"type": "string", "description": "show / delete: the recipe's name"}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_reminders",
            "description": "Reminders for the user: add sets one for a time (once or repeating) and it is delivered when due; list shows what is coming up; done marks one finished; snooze pushes it back; update changes its time; delete removes it. Use this whenever the user asks to be reminded of something. If no time was given, ask for one first.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["add", "list", "done", "snooze", "update", "delete"]},
                    "text": {"type": "string", "description": "What to be reminded of, in a few words — also how an existing reminder is found"},
                    "when": {"type": "string", "description": "The time on the user's own clock: ISO like 2026-11-23T15:00, or words like 'tomorrow at 3pm', 'in 2 hours'"},
                    "repeat": {"type": "string", "enum": ["none", "daily", "weekly", "monthly", "yearly"]},
                    "minutes": {"type": "integer", "description": "snooze: how long (default 60)"},
                    "new_text": {"type": "string", "description": "update: new wording"}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_trackers",
            "description": "Things the user follows day by day: a habit ticked off (kind check), an amount that adds up (kind number, e.g. glasses of water, with an optional daily goal) or a 1-5 rating (kind scale, e.g. mood). create starts a tracker; log records today's entry (or another day's); show gives the last two weeks and the streak; list summarises all of them.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["create", "log", "show", "list", "delete"]},
                    "tracker": {"type": "string", "description": "The tracker's name as the user says it"},
                    "kind": {"type": "string", "enum": ["check", "number", "scale"], "description": "create only"},
                    "unit": {"type": "string", "description": "create: for kind number, e.g. 'glasses', 'km'"},
                    "goal": {"type": "number", "description": "create: daily goal for kind number"},
                    "value": {"type": "number", "description": "log: the amount to add (number), the rating 1-5 (scale); omit for a check"},
                    "day": {"type": "string", "description": "log: 'today' (default), 'yesterday' or YYYY-MM-DD"},
                    "note": {"type": "string", "description": "log: a few words to keep with the entry"}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_boards",
            "description": "The user's boards: collections of links, pictures and notes around one idea (a room to redecorate, a party, recipes to try). pin saves one or more web links (the page's title and picture are fetched) or a note to a board by name, creating the board if needed; show lists what is on a board; list names the boards. To collect ideas, search the web first and then pin the best pages.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["pin", "create", "show", "list", "delete"]},
                    "board": {"type": "string", "description": "The board's name as the user says it"},
                    "urls": {"type": "array", "items": {"type": "string"}, "description": "pin: web addresses of pages or pictures"},
                    "text": {"type": "string", "description": "pin: a note to pin instead of (or with one) link"},
                    "title": {"type": "string", "description": "pin: a title for the pin"}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_projects",
            "description": "Code projects on this server (git repositories under the projects folder). create makes a new one, clone fetches one from a git URL or GitHub owner/repo, open ties an existing one to THIS chat — after create/clone/open the file tools and the shell work inside that project on every later message. list shows them all, status shows git state and what is deployed, close unties the chat.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "create", "clone", "open", "close", "status"]},
                    "name": {"type": "string", "description": "Project name (lower-case, dashes) or, for open/status, an absolute folder path"},
                    "url": {"type": "string", "description": "clone: https/ssh git URL, or owner/repo for GitHub"},
                    "description": {"type": "string", "description": "create: one line for the README"}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_apps",
            "description": "Host an app from a project so it keeps running: deploy turns a start command into a service that restarts after crashes and reboots and (for web apps) gets an HTTPS address reachable from the user's phone and PCs over Tailscale. Also list, status, logs, start, stop, restart, expose/unexpose, remove. The command must run the app in the foreground and listen on $PORT (127.0.0.1).",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "deploy", "status", "logs", "start", "stop", "restart", "expose", "unexpose", "remove"]},
                    "name": {"type": "string", "description": "App name (defaults to the project's name on deploy)"},
                    "command": {"type": "string", "description": "deploy: foreground start command, e.g. 'uv run uvicorn app:app --host 127.0.0.1 --port $PORT' or 'npm run start -- --port $PORT'"},
                    "project": {"type": "string", "description": "deploy: project name or folder (default: the project open in this chat)"},
                    "port": {"type": "integer", "description": "deploy: fixed port (default: next free one from 8300-8899)"},
                    "env": {"type": "object", "description": "deploy: extra environment variables (secrets belong in the project's .env file instead)"},
                    "web": {"type": "boolean", "description": "deploy: false for a worker/bot with no port (default true)"},
                    "lines": {"type": "integer", "description": "logs: how many lines (default 60)"}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_hosts",
            "description": "Other machines on the user's network, over SSH: run a command or a whole script on one (action run), copy files to or from it (copy), list registered hosts, list tailnet machines (peers), add/remove/test a host. Windows hosts run PowerShell — write normal multi-line PowerShell in command; Linux/macOS hosts run bash. Use this for anything on the user's other computers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "peers", "run", "copy", "add", "remove", "test", "workdir"]},
                    "host": {"type": "string", "description": "Host name from list ('local' is this server)"},
                    "path": {"type": "string", "description": "workdir: in Code mode on another machine, the folder there to work in from now on"},
                    "command": {"type": "string", "description": "run: the command or multi-line script"},
                    "shell": {"type": "string", "enum": ["bash", "powershell", "cmd"], "description": "run: override the host's default shell"},
                    "cwd": {"type": "string", "description": "run: folder on the host to run in"},
                    "timeout": {"type": "integer", "description": "run: seconds (default 120, max 3600)"},
                    "source": {"type": "string", "description": "copy: source path"},
                    "destination": {"type": "string", "description": "copy: destination path"},
                    "direction": {"type": "string", "enum": ["put", "get"], "description": "copy: put = this server to the host, get = host to this server"},
                    "address": {"type": "string", "description": "add: host name or IP"},
                    "user": {"type": "string", "description": "add: account to log in as"},
                    "os": {"type": "string", "enum": ["linux", "windows", "macos"], "description": "add: detected when omitted"}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_library",
            "description": "Add documents to the offline reference library (the survival library) so library_search finds them: PDF files from links on the internet (urls) or PDFs the user uploaded in this chat (files). Also list what was added, or remove one. Each added document is read page by page and is searchable at once; scanned ones are read with OCR in the background. scan takes in files the owner copied into the library folder on the server and files them on shelves; move puts a book or document on another shelf; shelves lists the shelves.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["add", "list", "remove", "sections", "scan", "shelves", "move"]},
                    "urls": {"type": "array", "items": {"type": "string"}, "description": "add: direct links to PDF files (a link to a web page is refused, with the PDF links found on it)"},
                    "as_page": {"type": "boolean", "description": "add: true keeps the web pages at urls themselves (their text, saved as documents) instead of expecting PDF files"},
                    "files": {"type": "array", "items": {"type": "string"}, "description": "add: uploaded PDFs, each by the path or id shown in the uploaded-files list"},
                    "section": {"type": "string", "description": "add: shelf to file it under, e.g. medical, survival, plants (action sections lists them); default general"},
                    "title": {"type": "string", "description": "add: title to file a single document under (otherwise taken from the document)"},
                    "document": {"type": "string", "description": "remove / move: the document's title or path"},
                    "shelf": {"type": "string", "description": "move: the shelf it should go on, e.g. 'History/Ancient world'"}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delegate_agent",
            "description": "Hand a coding task to another agent installed on this server — claude (Claude Code), kimi (Kimi Code), codex, opencode, or aider (local model) — working in a project folder. It runs in the background; you are called again with its output when it finishes. Use when the user asks for one of those agents by name, or for a large change better done by a dedicated coding agent. action agents lists what is installed; status/stop/list manage running hand-offs.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["agents", "run", "status", "stop", "list"]},
                    "agent": {"type": "string", "enum": ["claude", "kimi", "codex", "opencode", "aider"]},
                    "task": {"type": "string", "description": "run: the complete instruction for the other agent — it sees only this text and the project folder"},
                    "project": {"type": "string", "description": "run: project name or folder (default: the project open in this chat)"},
                    "autonomy": {"type": "string", "enum": ["edits", "full"], "description": "run: edits = may change files (default); full = may also run any command without asking. Use full only when the user asked for it."},
                    "continue": {"type": "boolean", "description": "run: continue that agent's previous session in the folder"},
                    "job": {"type": "string", "description": "status/stop: the job id"}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_bg_jobs",
            "description": "Inspect and control detached background `bash` jobs (started with the `#!bg` marker). action='list' shows this chat's jobs with id/status/age/command; action='output' returns a job's captured output so far (use for a still-running job, or to re-read a finished one); action='kill' terminates a runaway job's process tree instead of waiting out its max-runtime. output and kill need job_id from list.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "output", "kill"], "description": "list | output | kill (default: list)"},
                    "job_id": {"type": "string", "description": "Background job id (required for output/kill; from action='list')"},
                },
                "required": ["action"]
            }
        }
    },
]


# ---------------------------------------------------------------------------
# Converter: native function call -> ToolBlock
# ---------------------------------------------------------------------------

def _decode_loose_json_string(value: str) -> str:
    """Decode common JSON string escapes without requiring inner quotes to be escaped."""
    out = []
    i = 0
    while i < len(value):
        ch = value[i]
        if ch != "\\" or i + 1 >= len(value):
            out.append(ch)
            i += 1
            continue
        nxt = value[i + 1]
        if nxt == "n":
            out.append("\n")
        elif nxt == "r":
            out.append("\r")
        elif nxt == "t":
            out.append("\t")
        elif nxt == "b":
            out.append("\b")
        elif nxt == "f":
            out.append("\f")
        elif nxt in ('"', "\\", "/"):
            out.append(nxt)
        elif nxt == "u" and i + 5 < len(value):
            try:
                out.append(chr(int(value[i + 2:i + 6], 16)))
                i += 4
            except ValueError:
                out.append("\\" + nxt)
        else:
            out.append("\\" + nxt)
        i += 2
    return "".join(out)


def _repair_document_function_args(tool_type: str, arguments: str) -> Optional[dict]:
    """Salvage obvious malformed document tool args from local model wrappers.

    The doc LoRA sometimes emits the right native tool call but puts raw quotes
    inside the document text, making the surrounding JSON invalid. Treat that as
    a wrapper parse failure, not a semantic tool-choice failure.
    """
    if tool_type != "update_document" or not isinstance(arguments, str):
        return None
    raw = arguments.strip()
    if not raw.startswith("{") or not raw.endswith("}"):
        return None
    for key in ("content", "conten"):
        marker = f'"{key}"'
        key_pos = raw.find(marker)
        if key_pos < 0:
            continue
        colon_pos = raw.find(":", key_pos + len(marker))
        if colon_pos < 0:
            continue
        first_quote = raw.find('"', colon_pos + 1)
        if first_quote < 0:
            continue
        close_brace = raw.rfind("}")
        last_quote = raw.rfind('"', first_quote + 1, close_brace)
        if last_quote <= first_quote:
            continue
        content = _decode_loose_json_string(raw[first_quote + 1:last_quote])
        return {"content": content}
    return None


def function_call_to_tool_block(name: str, arguments: str) -> Optional[ToolBlock]:
    """Convert a native function call into a ToolBlock for the existing execution pipeline."""
    tool_type = _TOOL_NAME_MAP.get(name, name)
    try:
        if not arguments or (isinstance(arguments, str) and not arguments.strip()):
            args = {}
        else:
            args = json.loads(arguments) if isinstance(arguments, str) else arguments
    except (json.JSONDecodeError, TypeError):
        args = _repair_document_function_args(tool_type, arguments)
        if args is not None:
            logger.warning(f"Repaired malformed document function call arguments for {name}")
        else:
            logger.error(f"Failed to parse function call arguments for {name}: {arguments}")
            return None

    # Some models emit valid JSON that isn't an object (e.g. a bare array
    # ["ls -la"], string, or number) as function arguments. Most local tools keep
    # the legacy empty-object coercion for stream robustness, but email MCP tools
    # must fail closed so a malformed call cannot read the default mailbox.
    # Uses the shared BUILTIN_EMAIL_TOOLS (single source of truth) so the
    # fail-closed set can't drift from the dispatch/blocklist sets.
    if not isinstance(args, dict):
        if tool_type.startswith("mcp__email__") or name in BUILTIN_EMAIL_TOOLS:
            logger.warning(f"Non-object email function call arguments for {name}: {args!r}; rejecting")
            return None
        logger.warning(f"Non-object function call arguments for {name}: {args!r}; treating as empty")
        args = {}

    required_args = _REQUIRED_NATIVE_TOOL_ARGS.get(tool_type)
    if required_args and not any(str(args.get(key) or "").strip() for key in required_args):
        logger.warning(f"Rejecting empty required arguments for function call {name}: {args!r}")
        return None

    # Allow MCP tools through (namespaced as mcp__serverid__toolname)
    if tool_type.startswith("mcp__"):
        content = json.dumps(args) if args else "{}"
        return ToolBlock(tool_type, content)
    # Email tools are implemented as MCP — route them to email
    if name in BUILTIN_EMAIL_TOOLS:
        return ToolBlock(f"mcp__email__{name}", json.dumps(args) if args else "{}")
    if tool_type not in TOOL_TAGS:
        logger.warning(f"Unknown function call: {name}")
        return None

    # Convert structured args back to the text format each tool expects
    if tool_type == "bash":
        content = args.get("command", "")
    elif tool_type == "python":
        content = args.get("code", "")
    elif tool_type == "web_search":
        queries = args.get("queries")
        if isinstance(queries, list) and queries:
            content = str(queries[0])
        elif queries:
            content = str(queries)
        else:
            content = args.get("query", "")
        # Preserve the model-requested freshness filter — the web_search schema
        # advertises time_filter and the executor parses {"query","time_filter"},
        # but a bare query string dropped it. Mirrors the read_file JSON idiom.
        tf = args.get("time_filter")
        if content and isinstance(tf, str) and tf in ("day", "week", "month", "year"):
            content = json.dumps({"query": content, "time_filter": tf})
    elif tool_type == "read_file":
        # Plain path (back-compat) unless a line range is requested → JSON.
        if args.get("offset") or args.get("limit"):
            content = json.dumps(args)
        else:
            content = args.get("path", "")
    elif tool_type in ("grep", "glob", "ls"):
        content = json.dumps(args) if args else "{}"
    elif tool_type == "get_workspace":
        content = ""
    elif tool_type == "write_file":
        content = args.get("path", "") + "\n" + args.get("content", "")
    elif tool_type == "edit_file":
        content = json.dumps(args)
    elif tool_type == "create_document":
        parts = [args.get("title", "Untitled")]
        if args.get("language"):
            parts.append(args["language"])
        parts.append(args.get("content", ""))
        content = "\n".join(parts)
    elif tool_type == "edit_document":
        blocks = []
        edits = args.get("edits", [])
        # Native tool_calls deliver a real list; the XML/<invoke> path (and the
        # <function=> normalization) delivers the param as a JSON string. Parse
        # it so text-emitted edits aren't silently dropped (the call would run
        # with zero edits and look like a stall).
        if isinstance(edits, str):
            try:
                edits = json.loads(edits)
            except Exception:
                edits = []
        if isinstance(edits, dict):
            edits = [edits]
        if not isinstance(edits, list):
            edits = []
        for edit in edits:
            if not isinstance(edit, dict):
                continue
            blocks.append(
                f'<<<FIND>>>\n{edit.get("find", "")}\n<<<REPLACE>>>\n{edit.get("replace", "")}\n<<<END>>>'
            )
        content = "\n".join(blocks)
    elif tool_type == "suggest_document":
        blocks = []
        suggestions = args.get("suggestions", [])
        if isinstance(suggestions, str):
            try:
                suggestions = json.loads(suggestions)
            except Exception:
                suggestions = []
        if isinstance(suggestions, dict):
            suggestions = [suggestions]
        if not isinstance(suggestions, list):
            suggestions = []
        for s in suggestions:
            if not isinstance(s, dict):
                continue
            blocks.append(
                f'<<<FIND>>>\n{s.get("find", "")}\n<<<SUGGEST>>>\n{s.get("replace", "")}\n<<<REASON>>>\n{s.get("reason", "")}\n<<<END>>>'
            )
        content = "\n".join(blocks)
    elif tool_type == "update_document":
        content = args.get("content", "")
    elif tool_type == "search_chats":
        content = args.get("query", "")
    elif tool_type == "chat_with_model":
        content = args.get("model", "") + "\n" + args.get("message", "")
    elif tool_type == "create_session":
        content = args.get("name", "Untitled") + "\n" + args.get("model", "")
    elif tool_type == "list_sessions":
        content = args.get("filter", "")
    elif tool_type == "send_to_session":
        content = args.get("session_id", "") + "\n" + args.get("message", "")
    elif tool_type == "pipeline":
        # Pass as JSON for the pipeline parser
        content = json.dumps({"steps": args.get("steps", [])})
    elif tool_type == "manage_session":
        action = args.get("action", "")
        value = args.get("value", "")
        # `list` is the only action that takes an OPTIONAL keyword
        # filter — never a session_id. Don't leak the "current" default
        # into the filter slot (was producing "No sessions found
        # matching 'current'" when the agent omitted session_id).
        if action == "list":
            keyword = args.get("session_id", "") or args.get("keyword", "") or value
            content = "list" + (("\n" + keyword) if keyword and keyword.lower() != "current" else "")
        else:
            sid = args.get("session_id", "current")
            content = action + "\n" + sid
            if value:
                content += "\n" + value
    elif tool_type == "manage_memory":
        action = args.get("action", "")
        if action == "add":
            text = args.get("text") or args.get("value") or args.get("content") or ""
            if not text and args.get("key"):
                text = str(args.get("key") or "")
            content = "add\n" + str(text)
            if args.get("category"):
                content += "\n" + args["category"]
            elif args.get("key"):
                content += "\n" + str(args["key"])
        elif action == "edit":
            content = "edit\n" + args.get("memory_id", "") + "\n" + args.get("text", "")
        elif action == "delete":
            content = "delete\n" + args.get("memory_id", "")
        elif action == "search":
            content = "search\n" + (args.get("text") or args.get("tex") or args.get("query") or "")
        elif action == "list":
            content = "list"
            if args.get("category"):
                content += "\n" + args["category"]
        else:
            content = action
    elif tool_type == "generate_image":
        # do_generate_image (and the fenced prompt format) expect POSITIONAL
        # line format: prompt / model / size / quality. Native calls deliver a
        # JSON object, so serialize back to lines — keeping positions, since a
        # size with no model still needs an empty model line above it. Trailing
        # empties are dropped so a prompt-only call stays a single line.
        _ln = [
            str(args.get("prompt", "")).replace("\n", " "),
            str(args.get("model", "") or ""),
            str(args.get("size", "") or ""),
            str(args.get("quality", "") or ""),
        ]
        while len(_ln) > 1 and not _ln[-1]:
            _ln.pop()
        content = "\n".join(_ln)
    elif tool_type == "list_models":
        content = args.get("filter", "")
    elif tool_type == "ui_control":
        action = args.get("action", "")
        name = args.get("name", "")
        value = args.get("value", "")
        if action == "toggle":
            content = f"toggle {name} {value}"
        elif action == "open_panel":
            content = f"open_panel {name or value}"
        elif action == "open_email_reply":
            uid = args.get("uid") or name
            folder = args.get("folder") or value or "INBOX"
            mode = args.get("mode") or "reply"
            content = f"open_email_reply {uid} {folder} {mode}"
            body = args.get("body") or args.get("extra") or args.get("content") or ""
            if body:
                content += f" {body}"
        elif action == "set_mode":
            content = f"set_mode {value or name}"
        elif action == "switch_model":
            content = f"switch_model {value or name}"
        elif action == "set_theme":
            content = f"set_theme {value or name}"
        elif action == "create_theme":
            colors = args.get("colors", {})
            theme_name = name or value or "custom"
            bg = colors.get("bg", "#282c34")
            fg = colors.get("fg", "#9cdef2")
            panel = colors.get("panel", "#111111")
            border = colors.get("border", "#355a66")
            accent = colors.get("accent", "#e06c75")
            content = f"create_theme {theme_name} {bg} {fg} {panel} {border} {accent}"
            # Append advanced overrides as key=value
            adv_keys = [
                "userBubbleBg", "aiBubbleBg", "bubbleBorder", "sidebarBg",
                "sectionAccent", "brandColor", "inputBg", "inputBorder",
                "sendBtnBg", "sendBtnHover", "codeBg", "codeFg",
                "toggleBg", "toggleActive", "accentPrimary", "accentError",
            ]
            for ak in adv_keys:
                if colors.get(ak):
                    content += f" {ak}={colors[ak]}"
        else:
            content = action
    elif tool_type in ("manage_tasks", "manage_skills", "api_call",
                        "manage_endpoints", "manage_mcp", "manage_webhooks",
                        "manage_tokens", "manage_documents", "manage_settings"):
        content = json.dumps(args)
    elif tool_type == "ask_teacher":
        content = args.get("model", "auto") + "\n" + args.get("problem", "")
    elif tool_type == "ask_user":
        # Keep user-facing labels readable in the tool trace.  The outer SSE
        # JSON encoder will escape them for transport and JSON.parse restores
        # them once; pre-escaping here caused literal ``\u00f1`` sequences to
        # remain visible in the debug panel.
        content = json.dumps(args, ensure_ascii=False)
    else:
        content = json.dumps(args)

    return ToolBlock(tool_type, content)
