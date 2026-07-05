// iFinD 调用包装：call-node.js 直接运行会被 skill 守卫拦截（main 只打印提示），
// 需 require 后调用 call(serverType, toolName, params)。
// 用法: node ifind_call.js <serverType> <toolName> <paramsJson>
// 输出: 单行 JSON（call 的返回）
const path = require('path');
const os = require('os');
// iFinD skill 目录：优先环境变量 IFIND_DIR，默认用 ~/.workbuddy/skills/ 标准位置
const IFIND_DIR = process.env.IFIND_DIR || path.join(os.homedir(), '.workbuddy/skills/ifind-finance-data');
const ifind = require(path.join(IFIND_DIR, 'call-node.js'));

(async () => {
  const [, , serverType, toolName, paramsJson] = process.argv;
  if (!serverType || !toolName) {
    console.log(JSON.stringify({ ok: false, error: 'usage: node ifind_call.js <serverType> <toolName> <paramsJson>' }));
    process.exit(0);
  }
  try {
    const params = paramsJson ? JSON.parse(paramsJson) : {};
    const r = await ifind.call(serverType, toolName, params);
    console.log(JSON.stringify(r));
  } catch (e) {
    console.log(JSON.stringify({ ok: false, error: String(e && e.message || e) }));
  }
})();
