import {
  MAX_CASE_IDS,
  randomSeed,
  runSelectionLabel,
  selectionRequest,
  suiteSelection,
  type StoredSelection,
} from "../selection";

/** GSM8K 的题目勾选（sessionStorage 键 `motte.gsm8k.case-selection`）。 */
const store = suiteSelection("gsm8k");

export const loadCaseSelection = store.loadCaseSelection;
export const saveCaseSelection = store.saveCaseSelection;
export const clearCaseSelection = store.clearCaseSelection;
export { MAX_CASE_IDS, randomSeed, runSelectionLabel, selectionRequest };
export type { StoredSelection };
