import { useState } from 'react';
import { Modal } from '@/components/ui/modal';
import { getApiUrl } from '@/lib/api';
import { cn } from '@/lib/utils';
import { btnPrimary, formInput, formLabel } from '@/components/ui/formStyles';

interface ApiSettingsModalProps {
    isOpen: boolean;
    onClose: () => void;
}

export const ApiSettingsModal = ({ isOpen, onClose }: ApiSettingsModalProps) => {
  const [apiUrl, setApiUrl] = useState(getApiUrl());
  const [apiResponse, setApiResponse] = useState('');

  const handleApiCall = async () => {
    setApiResponse('Fetching...');
    try {
      const res = await fetch(`${apiUrl}/status`);
      if (!res.ok) throw new Error(`HTTP error! status: ${res.status}`);
      const data = await res.json();
      setApiResponse(JSON.stringify(data, null, 2));
    } catch (error) {
      setApiResponse(`Error fetching from API: ${error}`);
    }
  };

  return (
    <Modal isOpen={isOpen} onClose={onClose} title="Backend Settings">
        <div className="space-y-4">
            <div className="flex flex-col gap-1">
                <label htmlFor="api-url" className={cn(formLabel, 'select-none')}>Backend URL</label>
                <input
                id="api-url"
                value={apiUrl}
                onChange={(e) => {
                    const v = e.target.value;
                    setApiUrl(v);
                    const t = v.trim();
                    if (t) localStorage.setItem('apiUrl', t);
                    else localStorage.removeItem('apiUrl');
                }}
                className={cn(formInput, 'font-mono text-[0.75rem]')}
                placeholder="http://localhost:5001/api"
                />
            </div>

            <button onClick={handleApiCall} className={cn(btnPrimary, 'w-full')}>
                Test Connection
            </button>

            <div className="flex flex-col gap-1">
                <span className={cn(formLabel, 'select-none')}>Test Response</span>
                <pre className="h-24 overflow-auto whitespace-pre-wrap rounded-md border border-line bg-code-bg p-2 font-mono text-[0.75rem] text-ink-2">
                    {apiResponse || 'No response yet.'}
                </pre>
            </div>
        </div>
    </Modal>
  );
};
