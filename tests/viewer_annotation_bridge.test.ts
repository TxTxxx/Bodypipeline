import { expect, test } from "bun:test";
import { reviewAtoms, reviewPayload, insertReviewAtoms, type Review } from "../pipeline/server/viewer_ui/annotation-review-client";

const review: Review = { fps: 10, frames: 10, revision: 2, source_signature: "f".repeat(64),
  segments: [{start_frame:0,end_frame:2,text:"same"},{start_frame:5,end_frame:8,text:"same"}],
  atoms: [{role:"user",style:"vqa",content:"where?",timestamp:0.1,camera:"top",tool_calls:null}] };

test("AI intervals become ordinary subtask atoms beside manual VQA", () => {
  const atoms = reviewAtoms(review);
  expect(atoms.length).toBe(3);
  expect(atoms[1].end_timestamp).toBe(0.2);
  expect(atoms[2].timestamp).toBe(0.5);
  expect(reviewPayload(review, atoms).atoms).toEqual(review.atoms);
  expect(reviewPayload(review, atoms).segments).toEqual(review.segments);
});
test("editing and deleting an AI atom cannot modify another or fill gaps", () => {
  const atoms = reviewAtoms(review);
  atoms[1] = {...atoms[1], content:"human"};
  expect(reviewPayload(review, atoms).segments[1].text).toBe("same");
  atoms.splice(1,1);
  expect(reviewPayload(review, atoms).segments).toEqual([review.segments[1]]);
});
test("quick-add retains boundary insertion without filling preceding gaps", () => {
  const atoms = reviewAtoms(review);
  const next = insertReviewAtoms(atoms, [{...atoms[2],timestamp:0.6,end_timestamp:undefined,content:"new"}], review);
  expect(reviewPayload(review, next).segments).toEqual([
    review.segments[0],{start_frame:5,end_frame:6,text:"same"},{start_frame:6,end_frame:8,text:"new"}]);
  expect(atoms[2].end_timestamp).toBe(0.8);
});
test("empty and invalid ranges never silently restore source annotations", () => {
  expect(reviewPayload(review, []).segments).toEqual([]);
  const atoms=reviewAtoms(review);
  atoms[1]={...atoms[1],end_timestamp:0.7};
  expect(() => reviewPayload(review, atoms)).toThrow();
  atoms[1]={...atoms[1],timestamp:NaN};
  expect(() => reviewPayload(review, atoms)).toThrow();
});
