import { METHODS } from "./contract.js";

export function methodLabel(method: string): string {
  return METHODS.has(method) ? method : "other";
}
