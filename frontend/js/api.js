const fragment = new URLSearchParams(location.hash.slice(1));
if (fragment.has('token')) {
  sessionStorage.setItem('flowlens-token', fragment.get('token'));
  history.replaceState(null, '', location.pathname);
}
const token = sessionStorage.getItem('flowlens-token') || '';

export async function request(path, {body, query, blob = false} = {}) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query || {})) if (value !== null && value !== undefined) params.set(key, value);
  const response = await fetch(path + (params.size ? '?' + params : ''), {
    method: body ? 'POST' : 'GET', cache: 'no-store',
    headers: {Authorization: `Bearer ${token}`, ...(body ? {'Content-Type': 'application/json'} : {})},
    ...(body ? {body: JSON.stringify(body)} : {})
  });
  if (!response.ok) {
    const result = await response.json().catch(() => ({}));
    const error = new Error(result.error || `请求未完成（${response.status}）`);
    error.status = response.status;
    throw error;
  }
  return blob ? response.blob() : response.json();
}

export async function filePayload(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error('文件读取失败，请重新选择。'));
    reader.onload = () => resolve({name: file.name, data: String(reader.result).split(',')[1]});
    reader.readAsDataURL(file);
  });
}
