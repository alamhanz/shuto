# shuto web guide

This guide matches shuto version 0.4.0. Screens may differ slightly in other versions.

shuto is a web page that runs on your own computer. Open it by double-clicking the **shuto**
icon (or go to <http://127.0.0.1:8765>). The menu at the top has **Home**, **Add photos**,
**Settings** and **Help**. Every screen shows the next step.

## 1. First-time setup

The first time you open shuto, a short setup walks you through six screens. You can skip the
optional ones and come back later in **Settings**.

1. **Welcome.** A quick summary. Everything stays on your computer.
2. **AI helper.** shuto uses a free helper called Ollama to write titles and keywords. If it
   is missing, follow the steps shown for your computer, then click **Check again**. Prefer
   not to install it? Choose **Use an online AI instead** (your photos are then sent to that
   company). You then enter your access key for that AI (Anthropic or Gemini) or, for
   Bedrock, your AWS profile name. It is saved securely on this computer.
   <!-- screenshot: setup step 2, the AI helper (images/web-setup-helper.png) -->
3. **AI model.** Pick one of the suggested models. The cards say how long a photo takes and how
   big the download is. Downloads only start after you confirm.
   <!-- screenshot: setup step 3, choosing an AI model (images/web-setup-model.png) -->
4. **Shutterstock login (optional).** Enter your upload username and password and click
   **Save**. They are saved safely on this computer and never shown again. After the login is
   saved, you can click **Test login** in **Settings** to check that it works.
5. **Your style (optional).** Your theme, words to always add, and the tone you like. shuto
   starts with neutral settings. If you photograph in Japan, set **Region focus** to **Japan**
   to get romaji place names and Japan-first rules; the default is **None**.
6. **Done.** Click **Add your first photos**.

## 2. Add photos

1. Click **Add photos**.
2. Click **Choose folder**, move through your folders and click **Use this folder**. You can
   also paste the folder path.
   <!-- screenshot: choosing a folder (images/web-add-folder.png) -->
3. Give the batch a name (the folder name is suggested). A batch is a group of photos you
   work on together.
4. Click **Add photos**. When it finishes you see how many were added, how many look like
   photos you already sent, and how many could not be read. Click **Open batch**.

Your original photos are only read, never changed.

## 3. Check

Click **Check all photos**. shuto does three things in one go: it looks at quality (sharp,
bright and big enough), the AI writes a title, description and keywords, and shuto checks the
Shutterstock rules. A progress bar shows how long it will take, and you can cancel.
<!-- screenshot: the check step with a progress bar (images/web-check.png) -->

If the AI helper is not running, a box explains how to fix it in **Settings**.

### Your photo rules

In the same step the AI also answers three kinds of questions about each photo, with the same
model and no extra waiting: the **Shutterstock rules**, whether the photo fits your **theme**
(for example "Everyday Japan"), and **your own rules** (up to 10, such as "Must avoid: people").
Set them up in **Settings > Your photo rules**. For each rule you choose what happens when a
photo fails: **Set aside** or **just flag** it (**Needs a look**). A theme fit of "partly" is
let through and shown with its reason.

Changes only apply to photos you check from then on; photos already checked keep their results.
Every batch also has a **report** (a CSV file you can open in Excel) listing every check and
its reason: download it from Review or Submit, or press **Open folder** there. Each batch has
one folder in your workspace (`batches`) holding its report, its Shutterstock info files and the
photo copies that get uploaded.
Going back to shuto 0.3.x after updating to 0.4.0 is not possible, because the database is
upgraded.

## 4. Review

Here you decide what goes to Shutterstock. Each photo has a label:

| Label | Meaning |
|---|---|
| Looks good | Ready for you to approve |
| Needs a look | shuto noticed something, such as a logo or a missing release |
| Can't send as is | The photo breaks a rule and needs a change |
| Set aside | Too low quality, an exact copy, looks like another photo, or does not fit your theme or rules |
| Sent before | You already sent this photo |

<!-- screenshot: the review grid with labels (images/web-review.png) -->

- Use the filter chips at the top to show only one kind.
- A photo set aside because of your theme or a rule shows the reason. If you disagree, open it
  and press **Approve anyway**. (A photo that breaks a Shutterstock rule cannot be approved.)
- **Approve all that look good** approves the good ones in one click.
- Click a photo to read and edit its title, description and keywords. The page also explains
  what shuto noticed. **Approve** saves your edits first and then approves the photo, so you do not need to save
  separately. You can also use **Ask AI again** for a new text.
  <!-- screenshot: editing one photo (images/web-photo.png) -->
- Photos with visible logos or recognizable people can usually only be sold as **Editorial**.
  shuto suggests this and writes the caption in the editorial style.

When you are done, click **Go to Upload**.

## 5. Upload

Check the number of approved photos and your Shutterstock login, tick **Yes, upload these
photos to Shutterstock**, and click the upload button. shuto prepares copies of your photos
(your originals stay as they are), uploads them and creates the Shutterstock info files (CSV).
If something fails, a message says what happened and a **Try again** button appears.
<!-- screenshot: the upload step (images/web-upload.png) -->

## 6. Submit on Shutterstock

shuto never submits for you. The **Submit** page lists the steps:

1. Open the Shutterstock contributor site and sign in.
2. Upload the info file (CSV) you downloaded from shuto.
3. Check your photos and press **Submit**.
4. Back in shuto, click **I submitted this batch**.

<!-- screenshot: the submit step (images/web-submit.png) -->

Later, open a photo and record whether it was **Accepted** or **Rejected** (and why). shuto
uses the reasons to do better next time.

## Settings and Help

- **Settings:** change the AI model, your Shutterstock login (with **Test login**), your style
  (including **Region focus**: None or Japan), where AI models are kept (**Where to keep AI
  models**: pick another drive and shuto restarts Ollama and can move your downloaded models;
  Windows and Mac only), and see details of
  this computer. Access keys for online AI (Anthropic, Gemini) and the AWS profile for Bedrock
  go in the **Access key** field; they are saved securely, and **Forget** removes them. **Run setup again** repeats the first-time setup.
- **Updates (in Settings):** shows your version and the newest one. When a newer version is
  ready, press **Update now**; shuto closes, updates and opens again by itself, and this page
  reloads. **Check now** looks right away; the tick box turns the daily check off. It cannot
  start while a task is running.
- **Help:** plain explanations of the words used, what happens to your photos, and fixes for
  common problems.

## Uninstall

See [install.md](install.md#uninstall).
