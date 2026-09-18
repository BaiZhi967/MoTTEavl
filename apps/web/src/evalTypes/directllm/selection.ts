import { MAX_CASE_IDS, randomSeed, selectionRequest, suiteSelection,
  type StoredSelection } from "../selection";

/** Direct LLM 的题目勾选（sessionStorage 键 `motte.direct-llm.case-selection`）。 */
const store = suiteSelection("direct-llm");

export const loadCaseSelection = store.loadCaseSelection;
export const saveCaseSelection = store.saveCaseSelection;
export const clearCaseSelection = store.clearCaseSelection;
export { MAX_CASE_IDS, randomSeed, selectionRequest };
export type { StoredSelection };
