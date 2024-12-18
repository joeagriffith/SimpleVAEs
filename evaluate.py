import torch
import torch.nn as nn
import torch.nn.functional as F
from tqdm import tqdm
import numpy as np
from sklearn.neighbors import KNeighborsClassifier
from sklearn.metrics import classification_report
import pandas as pd
from functional import embed_dataset, get_balanced_subset


def evaluate(
        model,
        dataset,
):
    model.eval()
    embeddings, labels = embed_dataset(model, dataset)
    logs = linear_probing(model, embeddings, labels, n_train=200)
    logs['recon_loss'] = mse_dataset(model, dataset)
    return logs

def mse_dataset(model, dataset):
    device = next(model.parameters()).device
    batch_size = 256
    dataloader = torch.utils.data.DataLoader(dataset, batch_size=batch_size, shuffle=False)

    mse = 0.0
    for x, _ in dataloader:
        x = x.to(device)
        out = model(x)
        mse += model.loss(x, out)[1].item()
    return mse / len(dataset)


def knn_analysis(
        model,
        dataset,
		n_iter: int = 5,
        n_train: int = 200,
):
    embeddings, labels = embed_dataset(model, dataset)
    
    knn_defaults = dict(
        n_neighbors=5,
		metric='minkowski',
		weights='uniform',
		algorithm='auto',
		leaf_size=30,
        p=2,
    )

    accs = []
    for i in range(n_iter):
        train_x, train_y, test_x, test_y = get_balanced_subset(embeddings, labels, n_train)
        train_x = train_x.cpu().numpy()
        train_y = train_y.cpu().numpy()
        test_x = test_x.cpu().numpy()
        test_y = test_y.cpu().numpy()

        knn = KNeighborsClassifier(**knn_defaults).fit(
            train_x, train_y)
        report = classification_report(
            y_true=test_y,
            y_pred=knn.predict(test_x),
            output_dict=True,
            zero_division=0,
        )
        accs.append(report['accuracy'])
        
    return np.mean(accs).item()

def linear_probing(
    model,
    embeddings: torch.Tensor,
    labels: torch.Tensor,
    n_train: int,
):
    device = next(model.parameters()).device

    classifier = nn.Sequential(
        nn.Linear(model.z_dim, 10, bias=False),
    ).to(device)
    batch_size = max(n_train//10, 5)
    num_epochs = 100
    lr = 0.1
    wd = 0.005

    train_x, train_y, test_x, test_y = get_balanced_subset(embeddings, labels, n_train)

    param_dict = {pn: p for pn, p in classifier.named_parameters()}
    param_dict = {pn: p for pn, p in param_dict.items() if p.requires_grad}
    decay_params = [p for n, p in param_dict.items() if 'bn' not in n and 'bias' not in n]
    nondecay_params = [p for n, p in param_dict.items() if 'bn' in n or 'bias' in n]

    optim_groups = [
        {'params': decay_params, 'weight_decay': wd}, 
        {'params': nondecay_params, 'weight_decay': 0.0},
    ]

    optimiser = torch.optim.AdamW(optim_groups, lr=lr)
    sched_step_size = 30
    scheduler = torch.optim.lr_scheduler.StepLR(optimiser, step_size=sched_step_size, gamma=0.1) 

    logs = {
        'train_losses': [],
        'train_accs': [],
        'val_losses': [],
        'val_accs': [],
    }

    postfix = {}
    for epoch in range(num_epochs):
        classifier.train()

        # shuffle training data
        perm = torch.randperm(train_x.size(0))
        train_x = train_x[perm]
        train_y = train_y[perm]

        loop = tqdm(range(0, train_x.size(0), batch_size), total=len(train_x) // batch_size, leave=False)
        loop.set_description(f'Epoch [{epoch}/{num_epochs}]')
        if epoch > 0:
            postfix = {
                'loss': (round(logs['train_losses'][-1], 3), round(logs['val_losses'][-1], 3)),
                'accuracy': (round(logs['train_accs'][-1], 3), round(logs['val_accs'][-1], 3)),
            }
            loop.set_postfix(postfix)

        train_loss_total = 0.0
        train_acc_total = 0.0
        for lo in loop:
            hi = lo + batch_size
            x = train_x[lo:hi]
            y = train_y[lo:hi]

            x = x.to(device)
            y = y.to(device)
            y_pred = classifier(x)
            loss = F.cross_entropy(y_pred, y)
            train_acc_total += (y_pred.argmax(dim=1) == y).float().mean().detach().item()
                
            classifier.zero_grad(set_to_none=True)
            loss.backward()
            optimiser.step()

            train_loss_total += loss.detach().item()

        logs['train_losses'].append(train_loss_total / (len(train_x) / batch_size))
        logs['train_accs'].append(train_acc_total / (len(train_x) / batch_size))

        scheduler.step()
        
        with torch.no_grad():
            classifier.eval()

            test_loss_total = 0.0
            test_acc_total = 0.0
            for lo in range(0, test_x.size(0), batch_size):
                hi = lo + batch_size
                x = test_x[lo:hi]
                y = test_y[lo:hi]

                x = x.to(device)
                y = y.to(device)
                y_pred = classifier(x)
                loss = F.cross_entropy(y_pred, y)
                test_acc_total += (y_pred.argmax(dim=1) == y).float().mean().detach().item()
                test_loss_total += loss.detach().item()

            logs['val_losses'].append(test_loss_total / (len(test_x) / batch_size))
            logs['val_accs'].append(test_acc_total / (len(test_x) / batch_size))
        
    return logs
