import React from 'react';
import {createRoot} from 'react-dom/client';
import {BrowserRouter} from 'react-router-dom';
// React 19 适配：必须在任何 Semi 组件之前引入（Semi 2.103 官方适配器）
import '@douyinfe/semi-ui/react19-adapter';
// 顺序即优先级，别改：组件库 → Tailwind（preflight + 令牌）→ 控制台类层 → 主题层（最后载入）
import '@douyinfe/semi-ui/dist/css/semi.css';
import './tailwind.css';
import './ui.css';
import './theme.css';
import App from './App';

// 深色信息板是唯一主题（REDESIGN-PLAN.md §3.2）
document.body.setAttribute('theme-mode', 'dark');

createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <BrowserRouter>
      <App/>
    </BrowserRouter>
  </React.StrictMode>
);
