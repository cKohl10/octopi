import torch
from transformers import AutoConfig, AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
import torch.nn.functional as nnf
from tqdm import trange
import tqdm, json
from accelerate import init_empty_weights, infer_auto_device_map
import yaml
from datetime import datetime
import os
from peft import PeftModel, PeftConfig, get_peft_model, LoraConfig
from PIL import Image
from utils.model import MultimodalLLMForCausalLM
from utils.dataset import get_frames
from utils.promptclip import *
from transformers import CLIPImageProcessor
from transformers.utils import logging


def add_new_tokens(llm, tokenizer, new_tokens):
    new_tokens = list(set(new_tokens) - set(tokenizer.vocab.keys()))
    n_new_tokens = tokenizer.add_tokens(new_tokens)
    llm.resize_token_embeddings(len(tokenizer))


def sinusoidal_positional_embedding(token_sequence_size, indices, token_embedding_dim, batch_size, n=10000.0):
    # reference: https://pub.aimind.so/creating-sinusoidal-positional-embedding-from-scratch-in-pytorch-98c49e153d6
    if token_embedding_dim % 2 != 0:
        raise ValueError("Sinusoidal positional embedding cannot apply to odd token embedding dim (got dim={:d})".format(token_embedding_dim))
    positions = [indices]
    positions = torch.FloatTensor(positions).unsqueeze_(2) # [batch_size, sequence_len, 1]
    embeddings = torch.zeros(batch_size, token_sequence_size, token_embedding_dim) # [batch_size, sequence_len, embedding_dim]
    denominators = torch.pow(n, 2 * torch.arange(0, token_embedding_dim // 2) / token_embedding_dim) # 10000^(2i/d_model), i is the index of embedding --> [384]
    embeddings[:, :, 0::2] = torch.sin(positions / denominators) # sin(pos/10000^(2i/d_model)) # [batch_size, sequence_len, 384]
    embeddings[:, :, 1::2] = torch.cos(positions / denominators) # cos(pos/10000^(2i/d_model)) # [batch_size, sequence_len, 384]
    return embeddings


def process_user_input(user_input, image_processor, model, tokenizer, device):
    question_embeds = []
    for chunk in user_input:
        if "[" not in chunk:
            # Keep tokens as Long type for embedding layer
            tokens = torch.unsqueeze(torch.tensor(tokenizer.encode(chunk))[1:], 0).to(device)
            # Convert to float16 after embedding
            embeddings = model.llm.get_input_embeddings()(tokens).to(torch.float16)
            question_embeds.append(embeddings)
        else:
            tokens = torch.unsqueeze(torch.tensor(tokenizer.encode("<tact_start>"))[1:], 0).to(device)
            embeddings = model.llm.get_input_embeddings()(tokens).to(torch.float16)
            question_embeds.append(embeddings)
            
            frames, indices = get_frames(chunk[1:-1], image_processor, None, return_indices=True)
            tactile_tensors = torch.unsqueeze(frames, dim=0).to(device).to(torch.float16)
            sinusoidal_embeds = sinusoidal_positional_embedding(token_sequence_size=5, indices=indices, token_embedding_dim=1024, batch_size=tactile_tensors.shape[0]).to(tactile_tensors.device).to(torch.float16)
            tactile_embeds = model.project(model.encoder(tactile_tensors) + sinusoidal_embeds)
            question_embeds.append(tactile_embeds)
            
            tokens = torch.unsqueeze(torch.tensor(tokenizer.encode("<tact_end>"))[1:], 0).to(device)
            embeddings = model.llm.get_input_embeddings()(tokens).to(torch.float16)
            question_embeds.append(embeddings)
            
    question_embeds = torch.cat(question_embeds, dim=1)
    return question_embeds


def main(configs, exp_name):
    # device and seed
    device = f'cuda:{configs["cuda"]}' # for inputs and model if not device_map

    # load tokenizer and LLaMA weights
    if configs["model_type"] == "vicuna-7b":
        tokenizer_path = "lmsys/vicuna-7b-v1.5"
        model_path = "lmsys/vicuna-7b-v1.5"
    elif configs["model_type"] == "vicuna-13b":
        tokenizer_path = "lmsys/vicuna-13b-v1.5"
        model_path = "lmsys/vicuna-13b-v1.5"

    # model GPU setup (original)
    # bnb_config = BitsAndBytesConfig(
    #     load_in_4bit=True,
    #     bnb_4bit_use_double_quant=True,
    #     bnb_4bit_quant_type="nf4",
    #     bnb_4bit_compute_dtype=torch.float16,
    # )

    if configs["quantized"]:
        if configs["bnb_4bit"]:
            # 4-bit quantization setup
            bnb_config = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_use_double_quant=True,
                bnb_4bit_quant_type="nf4",
                bnb_4bit_compute_dtype=torch.float16,
                llm_int8_enable_fp32_cpu_offload=True
            )
        else:
            # 16-bit quantization setup
            bnb_config = BitsAndBytesConfig(
                load_in_8bit=False,
                load_in_4bit=False,
                bnb_4bit_compute_dtype=torch.float16
            )

    if configs["gpu_config"] is not None:
        if configs["tokenizer_path"] is not None:
            tokenizer_path = configs["tokenizer_path"]
        if not configs["lora_trained"]:
            if configs["llm_path"] is not None:
                model_path = configs["llm_path"]
        with init_empty_weights():
            config = AutoConfig.from_pretrained(model_path)
            auto_model = AutoModelForCausalLM.from_config(config)
        f = open(configs["gpu_config"])
        data = json.load(f)
        gpu_max_mem_config = {}
        for k, v in data.items():
            gpu_max_mem_config[int(k)] = v
        device_map = infer_auto_device_map(
            auto_model, max_memory = gpu_max_mem_config, no_split_module_classes=["LLaMADecoderLayer", "LlamaDecoderLayer"]
        )
        if configs["lora_trained"]:
            if configs["quantized"]:
                llm = AutoModelForCausalLM.from_pretrained(model_path, device_map=device_map, offload_folder=configs["offload_dir"], quantization_config=bnb_config)
            else:
                llm = AutoModelForCausalLM.from_pretrained(model_path, device_map=device_map, offload_folder=configs["offload_dir"])
            tokenizer = AutoTokenizer.from_pretrained(tokenizer_path, padding_side="left")
            # NOTE: https://jaotheboss.medium.com/domain-training-your-llm-6c77f53e3e27
            new_tokens = ['<tact_start>', '<tact_end>']
            add_new_tokens(llm, tokenizer, new_tokens)
            if configs["quantized"]:
                llm = PeftModel.from_pretrained(model=llm, model_id=configs["llm_path"], is_trainable=False, device_map="auto", max_memory=gpu_max_mem_config, quantization_config=bnb_config)
            else:
                llm = PeftModel.from_pretrained(model=llm, model_id=configs["llm_path"], is_trainable=False, device_map="auto", max_memory=gpu_max_mem_config)
        else:
            if configs["quantized"]:
                llm = AutoModelForCausalLM.from_pretrained(model_path, device_map=device_map, offload_folder=configs["offload_dir"], quantization_config=bnb_config)
            else:
                llm = AutoModelForCausalLM.from_pretrained(model_path, device_map=device_map, offload_folder=configs["offload_dir"])
            tokenizer = AutoTokenizer.from_pretrained(tokenizer_path, padding_side="left")

    if configs["lora_trained"]:
        model = MultimodalLLMForCausalLM(clip_model=configs["use_clip"], encoder_output_size=configs["encoder_output_size"], tokenizer=tokenizer, cutoff_len=configs["cutoff_len"], llm=llm.model, use_vqvae=configs["use_vqvae"], device=device)
    else:
        model = MultimodalLLMForCausalLM(clip_model=configs["use_clip"], encoder_output_size=configs["encoder_output_size"], tokenizer=tokenizer, cutoff_len=configs["cutoff_len"], llm=llm, use_vqvae=configs["use_vqvae"], device=device)
    model.to(device)
    model.llm = llm
    model.eval()
    # llm.eval()
    if configs["use_clip"]:
        image_processor = CLIPImageProcessor.from_pretrained(configs["use_clip"])
    if configs["encoder_path"] is not None:
        try:
            model.encoder.load_state_dict(torch.load(configs["encoder_path"]))
        except RuntimeError:
            clip = PromptLearningCLIPModel.from_pretrained(configs["use_clip"], configs).to(device)
            model.encoder.model.vision_model = clip.vision_model
            model.encoder.load_state_dict(torch.load(configs["encoder_path"]), strict=False)
        # Convert CLIP model to float16
        model.encoder = model.encoder.to(torch.float16)
    if configs["projection_path"] is not None:
        # if there is a trained encoder specified
        model.project.load_state_dict(torch.load(configs["projection_path"]))
        # Convert projection layer to float16
        model.project = model.project.to(torch.float16)

    # interact with LLM
    test_files = configs["test_files"]
    all_preds = []
    for file in test_files:
        f = open(file)
        data = json.load(f)
        with torch.no_grad():
            for d in tqdm.tqdm(data):
                if "avocado" in file:
                    question_type = "eval_avocado"
                else:
                    question_type = d[0]["question_type"]
                for q in range(2):
                    if q == 0:
                        prompt_pre = "A chat between a curious user and an artificial intelligence assistant. The assistant gives helpful, detailed, and polite answers to the user's questions.\n\nUSER: "
                        # Keep tokens as Long type for embedding layer
                        prompt_pre_tokens = torch.unsqueeze(torch.tensor(tokenizer.encode(prompt_pre))[1:], 0).to(device)
                        # Convert to float16 after embedding
                        prompt_pre_embeds_start = llm.get_input_embeddings()(prompt_pre_tokens).to(torch.float16)
                        prompt_post = f"\nASSISTANT:"
                        prompt_post_tokens = torch.unsqueeze(torch.tensor(tokenizer.encode(prompt_post))[1:], 0).to(device)
                        prompt_post_embeds = llm.get_input_embeddings()(prompt_post_tokens).to(torch.float16)
                        user_input = d[1]["content"]
                        tactile_images = d[1]["tactile"]
                        tactile_idx = 0
                        for c in range(len(user_input)):
                            if user_input[c] == "<img_tokens>":
                                user_input[c] = f"[{tactile_images[tactile_idx]}]"
                                tactile_idx += 1
                        question_embeds = process_user_input(user_input, image_processor, model, tokenizer, device)
                        input_embeds = torch.cat((prompt_pre_embeds_start, question_embeds, prompt_post_embeds), dim=1)
                        prev_embeds = input_embeds.detach().clone()
                        generation_tokens = torch.unsqueeze(model.llm.generate(inputs_embeds=input_embeds, max_new_tokens=configs["max_new_tokens"])[0, 1:], 0) # NOTE: right padding issue with </s>
                        generation = tokenizer.decode(generation_tokens[0]) # https://huggingface.co/docs/transformers/main/llm_tutorial
                        # check for USER: and truncate
                        generation = " " + generation.split("USER:")[0].strip().split("</s>")[0].strip()
                        if "</s>" not in generation:
                            generation += "</s>"
                        descriptions = generation[:]
                        generation_tokens = torch.unsqueeze(torch.tensor(tokenizer.encode(generation))[1:], 0).to(device)
                        # add generation embeddings to prev_embeds
                        generation_embeds = llm.get_input_embeddings()(generation_tokens)
                        prev_embeds = torch.cat([prev_embeds, generation_embeds], dim=1)
                    else:
                        prompt_pre = "\nUSER: "
                        prompt_pre_tokens = torch.unsqueeze(torch.tensor(tokenizer.encode(prompt_pre))[1:], 0).to(device) # NOTE: remove BOS token
                        prompt_pre_embeds = llm.get_input_embeddings()(prompt_pre_tokens)
                        prev_embeds = torch.cat([prev_embeds, prompt_pre_embeds], dim=1)
                        user_input = d[3]["content"]
                        question_embeds = process_user_input(user_input, image_processor, model, tokenizer, device)
                        input_embeds = torch.cat((prev_embeds, question_embeds, prompt_post_embeds), dim=1)
                        generation_tokens = torch.unsqueeze(model.llm.generate(inputs_embeds=input_embeds, max_new_tokens=configs["max_new_tokens"])[0, 1:], 0) # NOTE: right padding issue with </s>
                        generation = tokenizer.decode(generation_tokens[0]) # https://huggingface.co/docs/transformers/main/llm_tutorial
                        # check for USER: and truncate
                        generation = generation.split("USER:")[0].strip().split("</s>")[0].strip()
                        if "</s>" not in generation:
                            generation += "</s>"
                        # save generation
                all_preds.append({
                    "question": d[3]["content"],
                    "question_type": question_type,
                    "question_step": 2,
                    "descriptions": descriptions,
                    "sample_paths": tactile_images,
                    "answer": d[4]["content"],
                    "generation": generation,
                    })

    with open(f'{configs["exps_path"]}/{exp_name}/test_preds.json', 'w') as f:
        json.dump(all_preds, f, indent=4)
        f.close()


if __name__ == "__main__":
    exp_type = "test_two_step"
    config_path = f'configs/{exp_type}_config.yaml'
    # get configs
    with open(config_path, 'r') as file:
        configs = yaml.safe_load(file)
    exp_id = input("Identifier for experiment: ")
    if len(exp_id) > 0:
        exp_id = exp_type + f"_{exp_id}"
    else:
        exp_id = exp_type

    # make stats and weights folders
    now = datetime.now()
    exp_name = now.strftime("%Y_%m_%d_%H_%M_%S")
    exp_name = exp_name + "_" + exp_id
    print(f"\n{exp_name}\n")
    os.makedirs(f"{configs['exps_path']}", exist_ok=True)
    os.makedirs(f"{configs['exps_path']}/{exp_name}", exist_ok=True)
    with open(f"{configs['exps_path']}/{exp_name}/{exp_type}_config.yaml", 'w') as file:
        documents = yaml.dump(configs, file)
        file.close()

    logging.set_verbosity_error()
    main(configs, exp_name)