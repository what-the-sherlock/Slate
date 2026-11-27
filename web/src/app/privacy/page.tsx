import { ArrowLeft } from "lucide-react";
import Link from "next/link";
import type { ReactNode } from "react";
import { Logo } from "@/components/brand";
import { PanelLabel } from "@/components/panel";

export const metadata = { title: "Privacy policy" };

const CONTACT = "nusta.aashu@gmail.com";
const UPDATED = "27 November 2026";

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="mt-8">
      <h2 className="mb-2 text-ui font-semibold text-fg">{title}</h2>
      <div className="space-y-3 text-body text-fg-body">{children}</div>
    </section>
  );
}

/** Public privacy policy (linked from sign-in and required by Google's OAuth consent screen). */
export default function PrivacyPage() {
  const mail = (
    <a
      href={`mailto:${CONTACT}`}
      className="text-brand underline-offset-[3px] hover:underline"
    >
      {CONTACT}
    </a>
  );
  return (
    <div className="flex min-h-svh flex-col bg-subtle">
      <header className="flex h-14 items-center border-b border-border bg-bg px-4 sm:px-8">
        <div className="mx-auto flex w-full max-w-2xl items-center justify-between">
          <Logo />
          <Link
            href="/"
            className="inline-flex items-center gap-1 text-caption text-fg-muted hover:text-fg"
          >
            <ArrowLeft className="size-3.5" aria-hidden="true" />
            Back to Slate
          </Link>
        </div>
      </header>
      <main id="main" className="mx-auto w-full max-w-2xl px-4 py-12">
        <PanelLabel>Legal</PanelLabel>
        <h1 className="mt-1 mb-1 text-page font-semibold tracking-[-0.02em] text-fg">
          Privacy policy
        </h1>
        <p className="text-caption text-fg-subtle">Last updated {UPDATED}</p>

        <Section title="About Slate">
          <p>
            Slate is a personal portfolio project: a knowledge assistant that
            searches the sources you connect and answers questions with
            citations. It is run by an individual developer, not a company.
            Questions about this policy or your data: {mail}.
          </p>
        </Section>

        <Section title="What Slate stores">
          <ul className="list-disc space-y-1 pl-5">
            <li>
              Your account: email address, name, a hashed password, and your
              workspace memberships.
            </li>
            <li>
              Content you choose to connect or upload: files, GitHub documents,
              issues and pull requests, Discord messages from channels you
              select, and email conversations from mailboxes you connect. Slate
              stores the text, split into passages, with search indexes over it.
            </li>
            <li>Your chats with Slate, so you can come back to them.</li>
            <li>
              Sign-in tokens for connected services, encrypted at rest. They are
              never shown in the app or sent to anyone other than the service
              that issued them.
            </li>
            <li>
              Operational records: usage and cost counts, sync history and error
              codes. Logs never contain your document text, questions or
              answers.
            </li>
          </ul>
        </Section>

        <Section title="How it is used">
          <p>
            Only to provide Slate to you: searching your sources and answering
            your questions. Nothing is sold, used for advertising, or shared
            with other users. Connected sources are private to the person who
            connected them unless that person explicitly shares them with their
            workspace.
          </p>
        </Section>

        <Section title="Google user data">
          <p>
            If you connect a Google account, Slate requests read-only access
            (for Gmail, the <code className="text-data">gmail.readonly</code>{" "}
            scope). Slate uses this data only to index your email so you can
            search it and get cited answers in Slate. It never sends, changes or
            deletes email, and only you can see what it indexes.
          </p>
          <p>
            Slate&apos;s use and transfer of information received from Google
            APIs adheres to the{" "}
            <a
              href="https://developers.google.com/terms/api-services-user-data-policy"
              className="text-brand underline-offset-[3px] hover:underline"
            >
              Google API Services User Data Policy
            </a>
            , including the Limited Use requirements. Google user data is not
            used to train or improve AI models, not sold, not used for
            advertising, and not read by people except with your permission, for
            security, or where the law requires it.
          </p>
          <p>
            You can disconnect at any time from Integrations, or revoke access
            from your Google account&apos;s security settings. Disconnecting
            deletes the stored sign-in.
          </p>
        </Section>

        <Section title="Services that process data for Slate">
          <ul className="list-disc space-y-1 pl-5">
            <li>Vercel: hosts the website and API.</li>
            <li>
              Neon: the database and file storage, where your content and
              indexes live.
            </li>
            <li>
              OpenAI: turns passages into search vectors, and writes answers
              from the passages relevant to your question. Data sent through
              OpenAI&apos;s API is not used to train their models.
            </li>
            <li>GitHub Actions: runs background syncs.</li>
            <li>
              An email delivery provider: sends verification codes and
              invitations.
            </li>
            <li>Cloudflare Turnstile: protects sign-up from bots.</li>
          </ul>
        </Section>

        <Section title="Keeping and deleting your data">
          <p>
            Your data is kept while your account exists. Disconnecting or
            deleting a source removes its documents and indexes; leaving a
            workspace deletes what you connected there. To delete your account
            and everything in it, email {mail} from the address you signed up
            with, and it will be deleted within 30 days.
          </p>
        </Section>

        <Section title="Security">
          <p>
            Data is encrypted in transit, connection tokens are encrypted at
            rest, and every search is restricted in the database to content you
            are allowed to see. No system is perfectly secure, so connect only
            what you are comfortable storing in a portfolio project.
          </p>
        </Section>

        <Section title="Changes">
          <p>
            If this policy changes, the date above changes with it. Material
            changes will be announced in the app.
          </p>
        </Section>
      </main>
    </div>
  );
}
