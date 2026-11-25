// The steps under a reply, in plain words.
//
// A step is drawn with the tool's internal name (LIBRARY_SEARCH). That is one
// of "the workings": an administrator can switch them on in Settings,
// Appearance. For everyone else — and for the administrator until they do —
// each step says what was used instead ("The home library"), and still opens
// when tapped. The swap is done in CSS from the data-plain attribute this
// module keeps on every step, so nothing that draws a step had to change.

const PLAIN = {
  web_search: 'The web', web_fetch: 'A web page', library_search: 'The home library', library_browse: 'The home library shelves',
  manage_library: 'The home library', translate: 'Translator', convert_units: 'Almanac: units', map_position: 'Almanac: map',
  sun_moon: 'Almanac: sun and moon', date_math: 'Almanac: dates', weather: 'Weather forecast', identify_photo: 'Photo identification',
  manage_lists: 'Lists', manage_reminders: 'Reminders', manage_trackers: 'Trackers', manage_boards: 'Boards', manage_kitchen: 'Kitchen',
  manage_notes: 'Notes', manage_calendar: 'Calendar', manage_memory: 'Memory', manage_tasks: 'Scheduled', manage_skills: 'Skills',
  generate_image: 'Picture', trigger_research: 'Full report', manage_research: 'Reports', search_chats: 'Earlier chats',
  create_document: 'Document', edit_document: 'Document', update_document: 'Document', suggest_document: 'Document', manage_documents: 'Documents',
  bash: 'Shell', python: 'Python', read_file: 'File', write_file: 'File', edit_file: 'File', grep: 'Search in files', glob: 'Find files', ls: 'Folder',
  ask_user: 'A question for you', update_plan: 'Plan', ui_control: 'This screen', manage_projects: 'Project', manage_apps: 'Hosted app',
  manage_hosts: 'Another machine', delegate_agent: 'Handed off', api_call: 'A connected service', list_emails: 'Email', read_email: 'Email',
  send_email: 'Email', reply_to_email: 'Email', builtin_browser: 'Browser',
};

/** What a step is called when the workings are off. */
export function plainName(raw) {
  const text = String(raw || '').trim();
  const key = text.toLowerCase().replace(/\s+/g, '_');
  if (PLAIN[key]) return PLAIN[key];
  if (key.startsWith('mcp__')) return 'A connected tool';
  const words = text.replace(/_/g, ' ').toLowerCase();
  return words ? words[0].toUpperCase() + words.slice(1) : 'A step';
}

/** True when the name over a reply is a model's id ("gpt-oss-120b -> …", "qwen3.8-flash (quick)") and not a persona's name. */
export function isModelLabel(text) {
  const label = String(text || '').replace(/\(\s*(?:quick|careful|research)\s*\)/gi, '').trim();
  if (!label) return false;
  return label.includes('->') || (!/\s/.test(label) && /\d/.test(label));
}

function ownText(node) {
  return [...node.childNodes].filter((child) => child.nodeType === 3).map((child) => child.textContent).join(' ').replace(/\s+/g, ' ').trim();
}

function mark(root) {
  if (!root || !root.querySelectorAll) return;
  root.querySelectorAll('.agent-thread-tool').forEach((node) => {
    const plain = plainName(node.textContent);
    if (node.dataset.plain !== plain) node.dataset.plain = plain;
  });
  // The name over a reply: the assistant's, where the page would otherwise show which model answered.
  const name = (typeof window !== 'undefined' && window.__aegisAssistantName) || 'Aegis';
  root.querySelectorAll('.msg-ai .role').forEach((node) => {
    if (isModelLabel(ownText(node))) { if (node.dataset.plain !== name) node.dataset.plain = name; } else if (node.dataset.plain) delete node.dataset.plain;
  });
}

export function initWorkings() {
  const box = document.getElementById('chat-history') || document.body;
  mark(box);
  if (typeof MutationObserver !== 'function') return;
  let queued = false;
  new MutationObserver(() => {
    if (queued) return;
    queued = true;
    requestAnimationFrame(() => { queued = false; mark(box); });
  }).observe(box, { childList: true, subtree: true });
}

export default { initWorkings, plainName, isModelLabel };
