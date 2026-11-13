/**
 * OpenAI (and OpenAI-compatible: Ollama, DeepSeek, vLLM) chat completions and responses.
 *
 *     import { OpenAI } from "openai";
 *     import { instrument } from "agent-tracedown/openai";
 *
 *     const client = instrument(new OpenAI());
 *
 * Wraps `client.chat.completions.create` and `client.responses.create`, mirroring the
 * Python SDK's openai plugin. Duck-typed: this module never imports the openai package,
 * so any object with that shape can be instrumented, and the package stays an optional
 * peer dependency.
 *
 * Like the Python plugin, a wrapped call returns the recorded form of the reply
 * (`{ text, tool_calls, usage, finish_reason }`), not the SDK response object. Recording
 * a plain object is what makes the step replayable: on replay, `llmCall` serves the
 * recorded reply back without calling the API.
 */
import { llmCall } from "../tracer.js";

type Endpoint = { create: (...args: any[]) => Promise<any>; [k: string]: unknown };
type OpenAILike = {
  chat: { completions: Endpoint };
  responses?: Endpoint;
  [k: string]: unknown;
};

/** Turn a ChatCompletion into a JSON-safe dict: {text, tool_calls, usage, finish_reason}. */
export function normalizeChatCompletion(resp: any): any {
  if (!resp || typeof resp !== "object" || !Array.isArray(resp.choices)) return resp;
  const choice = resp.choices[0] ?? {};
  const msg = choice.message ?? {};
  const usage = resp.usage;
  return {
    text: msg.content ?? null,
    tool_calls: (msg.tool_calls ?? []).map((tc: any) => ({
      name: tc?.function?.name,
      arguments: tc?.function?.arguments,
    })),
    finish_reason: choice.finish_reason ?? null,
    usage: usage
      ? { input_tokens: usage.prompt_tokens ?? null, output_tokens: usage.completion_tokens ?? null }
      : null,
  };
}

/** Turn a Responses API response into the same recorded shape as a chat completion. */
export function normalizeResponse(resp: any): any {
  if (!resp || typeof resp !== "object") return resp;
  const toolCalls: { name: unknown; arguments: unknown }[] = [];
  const parts: string[] = [];
  for (const item of resp.output ?? []) {
    if (item?.type === "function_call") toolCalls.push({ name: item.name, arguments: item.arguments });
    for (const c of item?.content ?? []) if (typeof c?.text === "string") parts.push(c.text);
  }
  const usage = resp.usage;
  return {
    text: typeof resp.output_text === "string" && resp.output_text ? resp.output_text : parts.join("") || null,
    tool_calls: toolCalls,
    finish_reason: resp.status ?? null,
    usage: usage
      ? { input_tokens: usage.input_tokens ?? null, output_tokens: usage.output_tokens ?? null }
      : null,
  };
}

function wrapEndpoint(endpoint: Endpoint | undefined, kind: "chat" | "responses"): void {
  if (!endpoint || typeof endpoint.create !== "function") return;
  if ((endpoint as any)._tdnOriginal) return;
  const original = endpoint.create.bind(endpoint);
  const wrapped = async (params: any, options?: unknown) => {
    const call = async (prompt: unknown, callOpts: { model: string; temperature?: number }) => {
      const next = { ...(params ?? {}), model: callOpts.model };
      if (kind === "chat") next.messages = prompt;
      else next.input = prompt;
      if (callOpts.temperature !== undefined) next.temperature = callOpts.temperature;
      const resp = await original(next, options);
      return kind === "chat" ? normalizeChatCompletion(resp) : normalizeResponse(resp);
    };
    return llmCall(call as any, {
      prompt: kind === "chat" ? params?.messages : params?.input,
      model: params?.model ?? "unknown",
      provider: "openai",
      temperature: params?.temperature,
      seed: params?.seed,
    });
  };
  (endpoint as any)._tdnOriginal = original;
  endpoint.create = wrapped as Endpoint["create"];
}

/** Wrap an OpenAI client's create methods so each call is recorded as a model step. */
export function instrument<T extends OpenAILike>(client: T): T {
  if (!client || typeof client !== "object") return client;
  wrapEndpoint(client.chat?.completions, "chat");
  wrapEndpoint(client.responses, "responses");
  return client;
}

/** Restore the original create methods on an instrumented client. */
export function uninstrument<T extends OpenAILike>(client: T): T {
  for (const endpoint of [client?.chat?.completions, client?.responses]) {
    const original = (endpoint as any)?._tdnOriginal;
    if (endpoint && original) {
      endpoint.create = original;
      (endpoint as any)._tdnOriginal = undefined;
    }
  }
  return client;
}
