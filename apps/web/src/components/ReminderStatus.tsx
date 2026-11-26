import { useEffect, useState } from "react";
import type { Reminder } from "../contracts/metis";
import { reminderStatus } from "../lib/status";
import { StatusPill } from "./StatusPill";

export function ReminderStatus({ reminder }: { reminder: Reminder }) {
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);
  return <StatusPill status={reminderStatus(reminder, now)} />;
}
