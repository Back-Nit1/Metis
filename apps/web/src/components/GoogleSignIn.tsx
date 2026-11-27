import { useEffect, useRef, useState } from "react";

type GoogleIdentity = {
  initialize(config: { client_id: string; callback: (response: { credential: string }) => void }): void;
  renderButton(container: HTMLElement, options: { theme: string; size: string; width: number }): void;
};

function identity(): GoogleIdentity | undefined {
  return (window as Window & { google?: { accounts?: { id?: GoogleIdentity } } }).google?.accounts?.id;
}

let library: Promise<GoogleIdentity> | undefined;

function loadIdentity(): Promise<GoogleIdentity> {
  library ??= new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = "https://accounts.google.com/gsi/client";
    script.async = true;
    script.onload = () => {
      const client = identity();
      if (client) resolve(client);
      else reject(new Error("Google sign-in failed to initialize"));
    };
    script.onerror = () => reject(new Error("Google sign-in failed to load"));
    document.head.append(script);
  });
  return library;
}

export function GoogleSignIn({ clientId, disabled, onCredential }: {
  clientId: string;
  disabled: boolean;
  onCredential: (idToken: string) => void;
}) {
  const button = useRef<HTMLDivElement>(null);
  const deliver = useRef(onCredential);
  const [unavailable, setUnavailable] = useState(false);
  useEffect(() => { deliver.current = onCredential; }, [onCredential]);
  useEffect(() => {
    const container = button.current;
    if (!container) return;
    let active = true;
    void loadIdentity().then((client) => {
      if (!active) return;
      client.initialize({
        client_id: clientId,
        callback: ({ credential }) => { if (active && credential) deliver.current(credential); },
      });
      client.renderButton(container, { theme: "outline", size: "large", width: Math.min(container.clientWidth || 240, 320) });
    }).catch(() => { if (active) setUnavailable(true); });
    return () => { active = false; container.replaceChildren(); };
  }, [clientId]);
  return (
    <div className="mt-4 flex justify-center" hidden={disabled}>
      <div ref={button} className="w-full max-w-xs" />
      {unavailable && <p role="status" className="text-sm text-muted">Google sign-in could not load.</p>}
    </div>
  );
}
