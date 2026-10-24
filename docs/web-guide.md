# shuto web guide

This guide matches shuto version 0.6.5. Screens may differ slightly in other versions.

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
Cancel stops the AI within a second or two, even in the middle of a photo; that photo keeps
its old text. A slow model is only given up on when it sends nothing for 5 minutes.
<!-- screenshot: the check step with a progress bar (images/web-check.png) -->

If the AI helper is not running, a box explains how to fix it in **Settings**.

While the AI is working, the **Check all photos** button is greyed out and says
the AI is running. When you sent photos back and the AI is rewriting them, it also shows how
many are done. It works again when the AI is done.

Every model card says how well the model suits this computer: **Good**, **OK** or **Not
recommended**, with one line of reason. Only Good models are listed (the first one in the setup
is marked **Best match**). If none is Good, the best OK ones are listed with a note. Tick **Show
models that are not recommended for this computer** to see the rest; the tick is not remembered,
so the next visit starts with the short list again. The model you use is always shown. If it is
Not recommended, a warning says why and suggests a better one, but shuto never switches by
itself. Using or downloading a Not recommended model asks you to confirm first.

Models you downloaded yourself that are not on shuto's list show up too, if they can look at
photos, marked "Not on shuto's model list". They count as OK until shuto has seen a few photos
done with them. After a few photos, the rating uses how long your photos really took.
A model that keeps thinking before it answers, even though shuto asks it not to, is never rated
Good, because it is slow and sometimes stops without an answer.

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
one folder in your workspace (`batches`) holding its report, its Shutterstock info file and the
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
| Sent before | You already sent this photo; it carries a navy **Sent** chip and a **Sent** ribbon on its thumbnail |

<!-- screenshot: the review grid with labels (images/web-review.png) -->

- Use the filter chips at the top to show only one kind.
- A photo set aside because of your theme or a rule shows the reason. If you disagree, open it
  and press **Approve anyway**. (A photo that breaks a Shutterstock rule cannot be approved.)
- **Approve all that look good** approves the good ones in one click.
- Click a photo to read and edit its title, description and keywords. The page also explains
  what shuto noticed. **Approve** saves your edits first and then approves the photo, so you do not need to save
  separately. You can also use **Ask AI again** for a new text. **Send back to AI** starts the AI
  in the background. The photos leave the lists and come back one by one in their new group.
  You can keep reviewing the other photos while you wait; a note shows how many are still with the AI.
  The box **Run the AI right away** (shuto remembers your choice) decides whether the AI starts
  at once or only when you press **Check all photos**; until then the photos wait in the list.
- **Check again with AI** (photo page, for photos already sent to Shutterstock): the AI writes
  a new text as a draft and the photo keeps its place. Review marks it **New text to check**.
  Compare the two texts, then **Use the new text** or **Keep the old text**. If you use the new text,
  shuto rewrites the Shutterstock info file, and you upload that file again by hand on
  Shutterstock. shuto never submits for you; Submit stays manual.
  <!-- screenshot: editing one photo (images/web-photo.png) -->
- Photos with visible logos or recognizable people can usually only be sold as **Editorial**.
  shuto suggests this and writes the caption in the editorial style.

When you are done, click **Go to Upload**.

## 5. Upload

Check the number of approved photos and your Shutterstock login, tick **Yes, upload these
photos to Shutterstock**, and click the upload button. shuto prepares copies of your photos
(your originals stay as they are), uploads them and creates the Shutterstock info file (CSV), one per batch.
The CSV covers the photos that are approved and uploaded. If some photos are still being
reviewed, upload them later; they go into the next CSV.
If something fails, a message says what happened and a **Try again** button appears.
<!-- screenshot: the upload step (images/web-upload.png) -->

## 6. Submit on Shutterstock

shuto never submits for you. The **Submit** page lists the steps:

1. Open submit.shutterstock.com and sign in.
2. In the left menu, open **Portfolio, Submit content**. Your uploaded photos are on the
   **Not submitted** tab; it can take a few minutes after uploading for them to appear.
3. Click **Upload CSV** (top right) and choose the batch's info file
   (`<batch>-shutterstock.csv`, also in the batch folder shown on the page).
4. Check the photos, then press **Submit**. They move to **Portfolio, Pending content**.
   Editorial photos take no releases, and Usage must stay on Editorial (the info file sets it).
5. Back in shuto, click **I submitted this batch**.

<!-- screenshot: the submit step (images/web-submit.png) -->

Later, open a photo and record whether it was **Accepted** or **Rejected** (and why). shuto
uses the reasons to do better next time.

If you deleted photos on Shutterstock and want to upload them again, open **Deleted photos on
Shutterstock?** on the Upload or Submit page, tick them and confirm. See "If you deleted photos
on Shutterstock" in the details guide.

## Settings and Help

- **Settings:** change the AI model, your Shutterstock login (with **Test login**), your style
  (including **Region focus**: None or Japan), where AI models are kept (**Where to keep AI
  models**: pick another drive and shuto restarts Ollama and can move your downloaded models;
  Windows and Mac only), and see details of
  this computer. Access keys for online AI (Anthropic, Gemini) and the AWS profile for Bedrock
  go in the **Access key** field; they are saved securely, and **Forget** removes them. **Run setup again** repeats the first-time setup.
- **Updates (in Settings):** shows your version and the newest one. When a newer version is
  ready, press **Update now**; shuto closes, updates and opens again by itself, and this page
  reloads. **Check now** looks right away; shuto also looks by itself when it opens and every few hours; the tick box turns that off. It cannot
  start while a task is running.
- **Help:** plain explanations of the words used, what happens to your photos, and fixes for
  common problems. If shuto says another Ollama is answering (for example one in WSL or
  Docker), the entry "shuto says another Ollama is answering" has the steps.

## Uninstall

See [install.md](install.md#uninstall).
