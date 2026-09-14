import {ModelProfileEditor} from './components/ModelProfileEditor'; import {RunTimeline} from './components/RunTimeline'; import {ScoreTable} from './components/ScoreTable';
export default function App(){return <main><h1>MoTTEavl 评测控制台</h1><ModelProfileEditor profile={{input_modalities:['text'],supports_tools:false}}/><RunTimeline events={[]}/><ScoreTable scores={[]}/></main>}
