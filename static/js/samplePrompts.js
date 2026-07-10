/**
 * samplePrompts.js — a small library of example prompts for the `/prompt`
 * command, which drops a random one into the composer to break writer's block.
 *
 * Shape: { <category>: [ { prompt: string }, ... ] }. The `/prompt` handler
 * pulls from the chat-appropriate categories (chat/code/agent/html).
 */

export const EVAL_PROMPTS = {
  chat: [
    { prompt: 'Explain quantum entanglement to me like I am twelve, then like I am a physicist.' },
    { prompt: 'Help me plan a three-day trip to a city known for great food on a modest budget.' },
    { prompt: 'I want to start running. Build me a gentle four-week beginner plan.' },
    { prompt: 'Rewrite this sentence five ways, from very formal to very casual: "We need to talk."' },
    { prompt: 'Give me three dinner ideas using chicken, rice, and whatever is usually in a pantry.' },
    { prompt: 'What are some good questions to ask in a first one-on-one with a new manager?' },
  ],
  code: [
    { prompt: 'Write a Python function that debounces another function, with a short docstring and a test.' },
    { prompt: 'Explain the difference between a process and a thread, with a concrete example.' },
    { prompt: 'Refactor a callback-heavy JavaScript snippet to use async/await. Ask me for the snippet first.' },
    { prompt: 'Show me an idiomatic way to read a large file line by line in Python without loading it all.' },
    { prompt: 'Write a regular expression that matches an IPv4 address and explain each part.' },
  ],
  agent: [
    { prompt: 'Research the pros and cons of self-hosting an LLM versus using a hosted API, and summarize.' },
    { prompt: 'Find three recent, reputable articles on local-first software and give me the key takeaways.' },
    { prompt: 'Look up the current time zones for New York, London, and Tokyo and show them side by side.' },
    { prompt: 'Compare two popular open-source vector databases and recommend one for a small local project.' },
  ],
  html: [
    { prompt: 'Build a single-file HTML page with a live Markdown preview using only vanilla JavaScript.' },
    { prompt: 'Create a small HTML/CSS pricing table with three tiers and a highlighted middle plan.' },
    { prompt: 'Make an interactive HTML color-palette generator that copies hex codes on click.' },
  ],
};

export default EVAL_PROMPTS;
