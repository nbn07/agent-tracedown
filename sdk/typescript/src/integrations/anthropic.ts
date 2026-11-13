/**
 * Anthropic Messages API.
 *
 *     import Anthropic from "@anthropic-ai/sdk";
 *     import { instrument } from "agent-tracedown/anthropic";
 *
 *     const client = instrument(new Anthropic());
 *
 * Wraps `client.messages.create`, mirroring the Python SDK's anthropic plugin.
 * Duck-typed: this module never imports the SDK, so any object with that shape can
 * be instrumented, and the package stays an optional peer dependency.
 *
 * Like the Python plugin, a wrapped call returns the recorded form of the reply
 * (`{ text, tool_calls, usage, finish_reason }`), not the SDK Message object.
 * Recording a plain object is what makes the step replayable: on replay,
 * `llmCall` serves the recorded reply back without calling the API.
 */
import { llmCall } from "../tracer.js";

type MessagesEndpoint = { create: (...args: any[]) => Promise<any>; [k: string]: unknown };
type AnthropicLike = { messages: MessagesEndpoint; [k: string]: unknown };

/** Turn a Message into a JSON-safe dict: {text, tool_calls, usage, finish_reason}. */
export function normalizeMessage(resp: any): any {
  if (!resp || typeof resp !== "object") return resp;
  const blocks = Array.isArray(resp.content) ? resp.content : [];
  const usage = resp.usage;
  return {
    text: blocks.filter((b: any) => b?.type === "text").map((b: any) => b.text ?? "").join(""),
    tool_calls: blocks
      .filter((b: any) => b?.type === "tool_use")
      .map((b: any) => ({ name: b.name, arguments: b.input })),
    finish_reason: resp.stop_reason ?? null,
    usage: usage
      ? { input_tokens: usage.input_tokens ?? null, output_tokens: usage.output_tokens ?? null }
      : null,
  };
}

/** Wrap an Anthropic client's messages.create so each call is recorded as a model step. */
export function instrument<T extends AnthropicLike>(client: T): T {
  const messages = client?.messages;
  if (!messages || typeof messages.create !== "function") return client;
  if ((messages as any)._tdnOriginal) return client;
  const original = messages.create.bind(messages);
  const wrapped = async (params: any, options?: unknown) => {
    const call = async (prompt: any, callOpts: { model: string; temperature?: number }) => {
      const next = { ...(params ?? {}), model: callOpts.model, messages: prompt?.messages };
      if (prompt?.system !== undefined && prompt?.system !== null) next.system = prompt.system;
      if (callOpts.temperature !== undefined) next.temperature = callOpts.temperature;
      return normalizeMessage(await original(next, options));
    };
    return llmCall(call as any, {
      prompt: { system: params?.system, messages: params?.messages },
      model: params?.model ?? "unknown",
      provider: "anthropic",
      temperature: params?.temperature,
    });
  };
  (messages as any)._tdnOriginal = original;
  messages.create = wrapped as MessagesEndpoint["create"];
  return client;
}

/** Restore the original messages.create on an instrumented client. */
export function uninstrument<T extends AnthropicLike>(client: T): T {
  const messages = client?.messages;
  const original = (messages as any)?._tdnOriginal;
  if (messages && original) {
    messages.create = original;
    (messages as any)._tdnOriginal = undefined;
  }
  return client;
}
